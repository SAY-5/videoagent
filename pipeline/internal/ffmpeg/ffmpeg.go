// Package ffmpeg manages the FFmpeg subprocess. It builds the argv
// from the plan (mirroring videoagent.ffmpeg_run.build_argv on the
// Python side) and streams stderr through a ring buffer so the
// status endpoint can show progress without holding the full log
// in memory.

package ffmpeg

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"os/exec"
	"strconv"
	"strings"
	"sync"
)

// Op is a generic op shape — keys/values come from the planner's JSON.
// We treat them as `map[string]any` to avoid duplicating Python's
// Pydantic models on the Go side. The few fields we actually inspect
// are `op` (the verb) and a small set of numeric params.
type Op = map[string]any

// BuildArgv translates a plan into an FFmpeg argv. Mirrors
// videoagent.ffmpeg_run.build_argv on the Python side; tests in
// both languages assert the same canonical form.
func BuildArgv(input, output string, plan []Op) []string {
	argv := []string{"ffmpeg", "-y", "-i", input}

	var (
		videoFilters []string
		audioFilters []string
		needsConcat  []string
		seekArgs     []string
	)
	for _, op := range plan {
		kind, _ := op["op"].(string)
		switch kind {
		case "cut":
			s, _ := f64(op["start_s"])
			e, _ := f64(op["end_s"])
			videoFilters = append(videoFilters,
				fmt.Sprintf("select='not(between(t,%.3f,%.3f))',setpts=N/FRAME_RATE/TB", s, e))
			audioFilters = append(audioFilters,
				fmt.Sprintf("aselect='not(between(t,%.3f,%.3f))',asetpts=N/SR/TB", s, e))
		case "trim":
			ks, _ := f64(op["keep_start_s"])
			ke, _ := f64(op["keep_end_s"])
			seekArgs = []string{
				"-ss", fmt.Sprintf("%.3f", ks),
				"-t",  fmt.Sprintf("%.3f", ke-ks),
			}
		case "fade_in":
			at, _ := f64(op["at_s"])
			d, _ := f64(op["duration_s"])
			videoFilters = append(videoFilters, fmt.Sprintf("fade=t=in:st=%.3f:d=%.3f", at, d))
		case "fade_out":
			at, _ := f64(op["at_s"])
			d, _ := f64(op["duration_s"])
			videoFilters = append(videoFilters, fmt.Sprintf("fade=t=out:st=%.3f:d=%.3f", at, d))
		case "speed":
			f, _ := f64(op["factor"])
			videoFilters = append(videoFilters, fmt.Sprintf("setpts=%.6f*PTS", 1.0/f))
			audioFilters = append(audioFilters, atempoChain(f))
		case "volume":
			f, _ := f64(op["factor"])
			audioFilters = append(audioFilters, fmt.Sprintf("volume=%.3f", f))
		case "resize":
			w, _ := i64(op["width"])
			h, _ := i64(op["height"])
			videoFilters = append(videoFilters, fmt.Sprintf("scale=%d:%d", w, h))
		case "concat":
			if items, ok := op["inputs"].([]any); ok {
				for _, it := range items {
					if s, ok := it.(string); ok {
						needsConcat = append(needsConcat, s)
					}
				}
			}
		}
	}

	for _, src := range needsConcat {
		argv = append(argv, "-i", src)
	}
	if len(needsConcat) > 0 {
		n := 1 + len(needsConcat)
		var b strings.Builder
		for i := 0; i < n; i++ {
			fmt.Fprintf(&b, "[%d:v:0][%d:a:0]", i, i)
		}
		fmt.Fprintf(&b, "concat=n=%d:v=1:a=1[v][a]", n)
		argv = append(argv, "-filter_complex", b.String(), "-map", "[v]", "-map", "[a]")
	} else if len(videoFilters) > 0 || len(audioFilters) > 0 {
		var parts []string
		if len(videoFilters) > 0 {
			parts = append(parts, "[0:v:0]"+strings.Join(videoFilters, ",")+"[v]")
		}
		if len(audioFilters) > 0 {
			parts = append(parts, "[0:a:0]"+strings.Join(audioFilters, ",")+"[a]")
		}
		argv = append(argv, "-filter_complex", strings.Join(parts, ";"))
		if len(videoFilters) > 0 {
			argv = append(argv, "-map", "[v]")
		}
		if len(audioFilters) > 0 {
			argv = append(argv, "-map", "[a]")
		}
	}
	argv = append(argv, seekArgs...)
	argv = append(argv, output)
	return argv
}

func f64(v any) (float64, bool) {
	switch x := v.(type) {
	case float64:
		return x, true
	case float32:
		return float64(x), true
	case int:
		return float64(x), true
	case int64:
		return float64(x), true
	case string:
		f, err := strconv.ParseFloat(x, 64)
		return f, err == nil
	}
	return 0, false
}

func i64(v any) (int64, bool) {
	switch x := v.(type) {
	case float64:
		return int64(x), true
	case int:
		return int64(x), true
	case int64:
		return x, true
	case string:
		n, err := strconv.ParseInt(x, 10, 64)
		return n, err == nil
	}
	return 0, false
}

func atempoChain(factor float64) string {
	if factor >= 0.5 && factor <= 2.0 {
		return fmt.Sprintf("atempo=%.4f", factor)
	}
	if factor < 0.5 {
		return fmt.Sprintf("atempo=0.5,atempo=%.4f", factor/0.5)
	}
	parts := []string{}
	for factor > 2.0 {
		parts = append(parts, "atempo=2.0")
		factor /= 2.0
	}
	parts = append(parts, fmt.Sprintf("atempo=%.4f", factor))
	return strings.Join(parts, ",")
}

// Run executes ffmpeg with the given argv and streams stderr through
// `progress` (one line per call). Returns when the subprocess exits.
type Result struct {
	ExitCode int
	StderrTail string
}

func Run(ctx context.Context, argv []string, progress func(line string)) (Result, error) {
	if len(argv) == 0 {
		return Result{}, fmt.Errorf("ffmpeg: empty argv")
	}
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return Result{}, err
	}
	if err := cmd.Start(); err != nil {
		return Result{}, err
	}
	tail := newRingBuffer(64)
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		_ = streamLines(stderr, func(line string) {
			tail.push(line)
			if progress != nil {
				progress(line)
			}
		})
	}()
	werr := cmd.Wait()
	wg.Wait()
	exitCode := 0
	if werr != nil {
		if ee, ok := werr.(*exec.ExitError); ok {
			exitCode = ee.ExitCode()
		} else {
			return Result{}, werr
		}
	}
	return Result{ExitCode: exitCode, StderrTail: tail.dump()}, nil
}

func streamLines(r io.Reader, push func(string)) error {
	br := bufio.NewReader(r)
	for {
		line, err := br.ReadString('\n')
		if line != "" {
			push(strings.TrimRight(line, "\r\n"))
		}
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
	}
}

type ringBuffer struct {
	mu    sync.Mutex
	cap   int
	lines []string
}

func newRingBuffer(cap_ int) *ringBuffer {
	return &ringBuffer{cap: cap_}
}

func (r *ringBuffer) push(s string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.lines) >= r.cap {
		r.lines = r.lines[1:]
	}
	r.lines = append(r.lines, s)
}

func (r *ringBuffer) dump() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return strings.Join(r.lines, "\n")
}
