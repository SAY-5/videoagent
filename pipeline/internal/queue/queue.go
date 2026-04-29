// Package queue is the job queue. v1 ships an in-memory FIFO with a
// `pop with cancellation` semantics that mirrors Postgres's
// `SELECT … FOR UPDATE SKIP LOCKED` interface — the production
// adapter slots in at the same surface.

package queue

import (
	"context"
	"errors"
	"sync"
	"time"
)

// Job is one unit of work the pipeline executes.
type Job struct {
	ID         string            `json:"id"`
	SourceURL  string            `json:"source_url"`
	OutputKey  string            `json:"output_key"`
	Plan       []map[string]any  `json:"plan"`
	WebhookURL string            `json:"webhook_url,omitempty"`
	EnqueuedAt time.Time         `json:"enqueued_at"`
	Attempt    int               `json:"attempt"`
	Meta       map[string]string `json:"meta,omitempty"`
}

// Queue is implementation-agnostic.
type Queue interface {
	Enqueue(ctx context.Context, j Job) error
	Pop(ctx context.Context) (Job, error)
	Size() int
}

var ErrEmpty = errors.New("queue: empty")

// Memory is an in-memory FIFO with a condvar so Pop can block on
// cancellation. Goroutine-safe.
type Memory struct {
	mu  sync.Mutex
	cv  *sync.Cond
	buf []Job
}

func NewMemory() *Memory {
	m := &Memory{}
	m.cv = sync.NewCond(&m.mu)
	return m
}

func (m *Memory) Enqueue(_ context.Context, j Job) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if j.EnqueuedAt.IsZero() {
		j.EnqueuedAt = time.Now().UTC()
	}
	m.buf = append(m.buf, j)
	m.cv.Signal()
	return nil
}

// Pop blocks until a job is available or ctx is cancelled.
func (m *Memory) Pop(ctx context.Context) (Job, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	// Watch ctx and wake the waiter.
	stop := make(chan struct{})
	defer close(stop)
	go func() {
		select {
		case <-ctx.Done():
			m.mu.Lock()
			m.cv.Broadcast()
			m.mu.Unlock()
		case <-stop:
		}
	}()

	for len(m.buf) == 0 {
		if err := ctx.Err(); err != nil {
			return Job{}, err
		}
		m.cv.Wait()
	}
	j := m.buf[0]
	m.buf = m.buf[1:]
	return j, nil
}

func (m *Memory) Size() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.buf)
}
