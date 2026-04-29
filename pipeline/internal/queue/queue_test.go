package queue_test

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/SAY-5/videoagent/pipeline/internal/queue"
)

func TestEnqueueDequeueFIFO(t *testing.T) {
	q := queue.NewMemory()
	ctx := context.Background()
	for i, id := range []string{"a", "b", "c"} {
		_ = q.Enqueue(ctx, queue.Job{ID: id, Attempt: i})
	}
	for _, want := range []string{"a", "b", "c"} {
		got, err := q.Pop(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if got.ID != want {
			t.Fatalf("FIFO violated: got %s want %s", got.ID, want)
		}
	}
}

func TestPopBlocksUntilEnqueueOrCancel(t *testing.T) {
	q := queue.NewMemory()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var wg sync.WaitGroup
	wg.Add(1)
	got := make(chan string, 1)
	go func() {
		defer wg.Done()
		j, err := q.Pop(ctx)
		if err != nil {
			got <- "ERR:" + err.Error()
			return
		}
		got <- j.ID
	}()

	time.Sleep(40 * time.Millisecond)
	if err := q.Enqueue(ctx, queue.Job{ID: "x"}); err != nil {
		t.Fatal(err)
	}
	wg.Wait()
	if v := <-got; v != "x" {
		t.Fatalf("expected x, got %s", v)
	}
}

func TestPopReturnsOnContextCancellation(t *testing.T) {
	q := queue.NewMemory()
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	_, err := q.Pop(ctx)
	if err == nil {
		t.Fatal("expected ctx error, got nil")
	}
}

func TestSizeReportsBacklog(t *testing.T) {
	q := queue.NewMemory()
	for i := 0; i < 7; i++ {
		_ = q.Enqueue(context.Background(), queue.Job{ID: "j"})
	}
	if q.Size() != 7 {
		t.Fatalf("expected 7, got %d", q.Size())
	}
}
