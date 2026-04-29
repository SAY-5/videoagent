// Package upload abstracts the output sink. v1 ships a local-disk
// implementation; the production swap is the AWS S3 SDK at the same
// surface. The Uploader is what the pipeline calls after FFmpeg
// finishes successfully.

package upload

import (
	"context"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"sync/atomic"
)

// Uploader writes a local file to a stable destination and returns a
// URL clients can use to download it.
type Uploader interface {
	// Upload reads `localPath` and persists it under `key`. The
	// returned URL must remain valid for at least an hour (S3
	// presigning behavior).
	Upload(ctx context.Context, localPath, key string) (string, error)

	// Stats reports operational counters for /metrics.
	Stats() (uploaded int64, bytes int64)
}

// Local writes outputs to a base directory and returns file:// URLs.
// Used for tests + single-host deployments.
type Local struct {
	BaseDir string

	uploaded atomic.Int64
	bytes    atomic.Int64
}

func NewLocal(baseDir string) (*Local, error) {
	if err := os.MkdirAll(baseDir, 0o755); err != nil {
		return nil, fmt.Errorf("upload: mkdir %s: %w", baseDir, err)
	}
	return &Local{BaseDir: baseDir}, nil
}

func (l *Local) Upload(_ context.Context, localPath, key string) (string, error) {
	if key == "" {
		return "", fmt.Errorf("upload: empty key")
	}
	dst := filepath.Join(l.BaseDir, key)
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return "", err
	}
	src, err := os.Open(localPath)
	if err != nil {
		return "", err
	}
	defer src.Close()
	out, err := os.Create(dst)
	if err != nil {
		return "", err
	}
	defer out.Close()
	n, err := io.Copy(out, src)
	if err != nil {
		return "", err
	}
	l.uploaded.Add(1)
	l.bytes.Add(n)
	abs, _ := filepath.Abs(dst)
	return (&url.URL{Scheme: "file", Path: abs}).String(), nil
}

func (l *Local) Stats() (int64, int64) {
	return l.uploaded.Load(), l.bytes.Load()
}
