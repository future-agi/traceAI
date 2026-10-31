package auto_test

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"reflect"
	"testing"
)

func TestCachedTracerSurvivesAutomaticInit(t *testing.T) {
	hostReady(t)
	bin := filepath.Join(t.TempDir(), "cached")
	build := hostCommand("go", "build", "-o", bin, "./testdata/cached")
	build.Env = cleanEnv()
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build cached tracer app: %v %s", err, out)
	}
	cmd := hostCommand(bin)
	cmd.Dir = t.TempDir()
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.Header().Set("Content-Type", "application/x-protobuf")
	}))
	defer sink.Close()
	cmd.Env = append(cleanEnv(), "FI_API_KEY=DUMMY_FI_API_KEY", "FI_SECRET_KEY=DUMMY_FI_SECRET_KEY", "FI_BASE_URL="+sink.URL)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("cached tracer app: %v %s", err, out)
	}
	var got struct {
		Replaced  bool     `json:"provider_replaced"`
		Recording bool     `json:"cached_recording"`
		Names     []string `json:"names"`
	}
	if err := json.Unmarshal(out, &got); err != nil {
		t.Fatalf("invalid child output: %v %s", err, out)
	}
	if got.Replaced || !got.Recording || !reflect.DeepEqual(got.Names, []string{"cached-application-span", "fresh-application-span"}) {
		t.Fatalf("cached tracer disconnected: %s", out)
	}
}
