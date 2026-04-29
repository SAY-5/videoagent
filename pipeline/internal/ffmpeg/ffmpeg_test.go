package ffmpeg_test

import (
	"strings"
	"testing"

	"github.com/SAY-5/videoagent/pipeline/internal/ffmpeg"
)

func TestBuildArgvCutEmitsSelectFilter(t *testing.T) {
	argv := ffmpeg.BuildArgv("in.mp4", "out.mp4", []ffmpeg.Op{
		{"op": "cut", "start_s": 0.0, "end_s": 10.0},
	})
	joined := strings.Join(argv, " ")
	if !strings.Contains(joined, "select='not(between(t,0.000,10.000))'") {
		t.Fatalf("missing video select filter: %s", joined)
	}
	if !strings.Contains(joined, "[0:v:0]") || !strings.Contains(joined, "[v]") {
		t.Fatalf("filter_complex video map missing: %s", joined)
	}
}

func TestBuildArgvFadeFiltersChain(t *testing.T) {
	argv := ffmpeg.BuildArgv("in.mp4", "out.mp4", []ffmpeg.Op{
		{"op": "fade_in",  "at_s": 0.0, "duration_s": 1.0},
		{"op": "fade_out", "at_s": 5.0, "duration_s": 1.0},
	})
	joined := strings.Join(argv, " ")
	if !strings.Contains(joined, "fade=t=in:st=0.000:d=1.000") {
		t.Fatalf("missing fade in: %s", joined)
	}
	if !strings.Contains(joined, "fade=t=out:st=5.000:d=1.000") {
		t.Fatalf("missing fade out: %s", joined)
	}
}

func TestBuildArgvSpeedChainsAtempoFactorsOver2(t *testing.T) {
	argv := ffmpeg.BuildArgv("in.mp4", "out.mp4", []ffmpeg.Op{
		{"op": "speed", "factor": 4.0},
	})
	joined := strings.Join(argv, " ")
	// 4x = atempo=2.0,atempo=2.0
	if !strings.Contains(joined, "atempo=2.0,atempo=2.0000") {
		t.Fatalf("atempo chain missing: %s", joined)
	}
	if !strings.Contains(joined, "setpts=0.250000*PTS") {
		t.Fatalf("setpts missing: %s", joined)
	}
}

func TestBuildArgvResizeEmitsScale(t *testing.T) {
	argv := ffmpeg.BuildArgv("in.mp4", "out.mp4", []ffmpeg.Op{
		{"op": "resize", "width": 1280, "height": 720},
	})
	joined := strings.Join(argv, " ")
	if !strings.Contains(joined, "scale=1280:720") {
		t.Fatalf("scale filter missing: %s", joined)
	}
}

func TestBuildArgvVolumeOnly(t *testing.T) {
	argv := ffmpeg.BuildArgv("in.mp4", "out.mp4", []ffmpeg.Op{
		{"op": "volume", "factor": 0.5},
	})
	joined := strings.Join(argv, " ")
	if !strings.Contains(joined, "volume=0.500") {
		t.Fatalf("volume missing: %s", joined)
	}
	// Volume-only edits should not emit a video filter chain.
	if strings.Contains(joined, "[0:v:0]") {
		t.Fatalf("unexpected video filter for audio-only edit: %s", joined)
	}
}

func TestBuildArgvConcatAddsExtraInputsAndConcatFilter(t *testing.T) {
	argv := ffmpeg.BuildArgv("a.mp4", "out.mp4", []ffmpeg.Op{
		{"op": "concat", "inputs": []any{"b.mp4", "c.mp4"}},
	})
	joined := strings.Join(argv, " ")
	if !strings.Contains(joined, "-i b.mp4") || !strings.Contains(joined, "-i c.mp4") {
		t.Fatalf("extra -i inputs missing: %s", joined)
	}
	if !strings.Contains(joined, "concat=n=3:v=1:a=1") {
		t.Fatalf("concat filter missing: %s", joined)
	}
}

func TestBuildArgvTrimUsesSeekFlags(t *testing.T) {
	argv := ffmpeg.BuildArgv("in.mp4", "out.mp4", []ffmpeg.Op{
		{"op": "trim", "keep_start_s": 5.0, "keep_end_s": 12.0},
	})
	joined := strings.Join(argv, " ")
	if !strings.Contains(joined, "-ss 5.000") || !strings.Contains(joined, "-t 7.000") {
		t.Fatalf("trim seek args missing: %s", joined)
	}
}

func TestBuildArgvOutputPathIsLast(t *testing.T) {
	argv := ffmpeg.BuildArgv("in.mp4", "/tmp/output.mp4", []ffmpeg.Op{
		{"op": "fade_in", "at_s": 0.0, "duration_s": 1.0},
	})
	if argv[len(argv)-1] != "/tmp/output.mp4" {
		t.Fatalf("output not last arg: %v", argv)
	}
}
