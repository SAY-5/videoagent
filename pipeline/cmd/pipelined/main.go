// pipelined — the Go side of the videoagent pipeline.
//
// Pulls jobs from the queue, runs ffmpeg, uploads the output, marks
// the job done. The Python API enqueues + polls; this binary does
// the actual work.

package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"github.com/SAY-5/videoagent/pipeline/internal/ffmpeg"
	"github.com/SAY-5/videoagent/pipeline/internal/queue"
	"github.com/SAY-5/videoagent/pipeline/internal/upload"
)

type jobResult struct {
	ID         string `json:"id"`
	OK         bool   `json:"ok"`
	OutputURL  string `json:"output_url,omitempty"`
	StderrTail string `json:"stderr_tail,omitempty"`
	Error      string `json:"error,omitempty"`
	ElapsedMS  int64  `json:"elapsed_ms"`
}

func main() {
	addr := flag.String("addr", ":8090", "HTTP address for status + enqueue")
	outDir := flag.String("out", "./pipeline-out", "local output directory")
	flag.Parse()

	q := queue.NewMemory()
	up, err := upload.NewLocal(*outDir)
	if err != nil {
		log.Fatalf("upload: %v", err)
	}

	results := newResultsStore()

	ctx, cancel := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	wg.Add(1)
	go runWorker(ctx, &wg, q, up, results)

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Write([]byte(`{"ok":true}`)) //nolint:errcheck
	})
	mux.HandleFunc("/v1/jobs", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST required", http.StatusMethodNotAllowed)
			return
		}
		var j queue.Job
		if err := json.NewDecoder(r.Body).Decode(&j); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		if err := q.Enqueue(r.Context(), j); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		w.WriteHeader(http.StatusAccepted)
		_ = json.NewEncoder(w).Encode(map[string]any{"queued": j.ID})
	})
	mux.HandleFunc("/v1/jobs/", func(w http.ResponseWriter, r *http.Request) {
		id := r.URL.Path[len("/v1/jobs/"):]
		if id == "" {
			http.Error(w, "id required", http.StatusBadRequest)
			return
		}
		res, ok := results.get(id)
		if !ok {
			http.Error(w, "not found", http.StatusNotFound)
			return
		}
		_ = json.NewEncoder(w).Encode(res)
	})
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, _ *http.Request) {
		uploaded, bytes := up.Stats()
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		fmt.Fprintf(w, "videoagent_queue_size %d\n", q.Size())
		fmt.Fprintf(w, "videoagent_uploaded_total %d\n", uploaded)
		fmt.Fprintf(w, "videoagent_uploaded_bytes_total %d\n", bytes)
	})

	srv := &http.Server{Addr: *addr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		log.Printf("pipelined listening %s; output dir %s", *addr, *outDir)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatal(err)
		}
	}()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	cancel()
	ctx2, cancel2 := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel2()
	_ = srv.Shutdown(ctx2)
	wg.Wait()
}

func runWorker(
	ctx context.Context, wg *sync.WaitGroup,
	q *queue.Memory, up upload.Uploader, results *resultsStore,
) {
	defer wg.Done()
	for {
		j, err := q.Pop(ctx)
		if err != nil {
			return
		}
		t0 := time.Now()
		argv := ffmpeg.BuildArgv(j.SourceURL, "/tmp/"+j.ID+".mp4", castOps(j.Plan))
		r, err := ffmpeg.Run(ctx, argv, nil)
		if err != nil {
			results.set(j.ID, jobResult{ID: j.ID, OK: false, Error: err.Error()})
			continue
		}
		if r.ExitCode != 0 {
			results.set(j.ID, jobResult{
				ID: j.ID, OK: false,
				Error:      fmt.Sprintf("ffmpeg exit %d", r.ExitCode),
				StderrTail: r.StderrTail,
				ElapsedMS:  time.Since(t0).Milliseconds(),
			})
			continue
		}
		key := filepath.Join("jobs", j.ID, "out.mp4")
		url, uerr := up.Upload(ctx, "/tmp/"+j.ID+".mp4", key)
		if uerr != nil {
			results.set(j.ID, jobResult{
				ID: j.ID, OK: false,
				Error: uerr.Error(), StderrTail: r.StderrTail,
				ElapsedMS: time.Since(t0).Milliseconds(),
			})
			continue
		}
		results.set(j.ID, jobResult{
			ID: j.ID, OK: true, OutputURL: url,
			StderrTail: r.StderrTail,
			ElapsedMS:  time.Since(t0).Milliseconds(),
		})
	}
}

func castOps(in []map[string]any) []ffmpeg.Op {
	out := make([]ffmpeg.Op, len(in))
	for i, m := range in {
		out[i] = ffmpeg.Op(m)
	}
	return out
}

// in-memory results store, single-process. v2 swaps in Postgres.
type resultsStore struct {
	mu sync.RWMutex
	m  map[string]jobResult
}

func newResultsStore() *resultsStore { return &resultsStore{m: map[string]jobResult{}} }

func (r *resultsStore) set(id string, v jobResult) {
	r.mu.Lock(); defer r.mu.Unlock()
	r.m[id] = v
}

func (r *resultsStore) get(id string) (jobResult, bool) {
	r.mu.RLock(); defer r.mu.RUnlock()
	v, ok := r.m[id]
	return v, ok
}
