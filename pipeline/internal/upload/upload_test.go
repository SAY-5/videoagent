package upload_test

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/SAY-5/videoagent/pipeline/internal/upload"
)

func TestLocalUploadCopiesFileAndReturnsFileURL(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "src.bin")
	if err := os.WriteFile(src, []byte("hello"), 0o644); err != nil {
		t.Fatal(err)
	}
	u, err := upload.NewLocal(filepath.Join(dir, "out"))
	if err != nil {
		t.Fatal(err)
	}
	url, err := u.Upload(context.Background(), src, "jobs/abc/out.bin")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(url, "file://") {
		t.Fatalf("expected file:// url, got %q", url)
	}
	dst := filepath.Join(dir, "out", "jobs", "abc", "out.bin")
	got, err := os.ReadFile(dst)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "hello" {
		t.Fatalf("contents differ: %q", got)
	}
	uploaded, bytes := u.Stats()
	if uploaded != 1 || bytes != 5 {
		t.Fatalf("stats: uploaded=%d bytes=%d", uploaded, bytes)
	}
}

func TestLocalUploadCreatesNestedDirs(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "x")
	_ = os.WriteFile(src, []byte("z"), 0o644)
	u, _ := upload.NewLocal(filepath.Join(dir, "out"))
	if _, err := u.Upload(context.Background(), src, "deeply/nested/path/x"); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dir, "out/deeply/nested/path/x")); err != nil {
		t.Fatal(err)
	}
}

func TestLocalUploadEmptyKeyRejected(t *testing.T) {
	u, _ := upload.NewLocal(t.TempDir())
	if _, err := u.Upload(context.Background(), "anything", ""); err == nil {
		t.Fatal("expected error on empty key")
	}
}
