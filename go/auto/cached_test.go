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

// With FI keys in the environment, package initialization must not install a
// global provider. A tracer the application cached at package level follows
// the provider the application installs in main, and an explicit Init before
// the first AI call keeps its options.
func TestCachedTracerFollowsApplicationProvider(t *testing.T) {
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
		t.Fatalf("cached tracer app: %v %s %s", err, out, stderr.String())
	}
	var got struct {
		Installed   bool     `json:"agent_installed_global"`
		Application []string `json:"application"`
		Project     string   `json:"project"`
	}
	if err := json.Unmarshal(out, &got); err != nil {
		t.Fatalf("invalid child output: %v %s", err, out)
	}
	if got.Installed {
		t.Fatalf("package initialization installed a global tracer provider: %s", out)
	}
	if !reflect.DeepEqual(got.Application, []string{"cached-application-span", "fresh-application-span"}) {
		t.Fatalf("the application's own provider lost spans: %s", out)
	}
	if got.Project != "explicit-project" {
		t.Fatalf("an explicit Init before the first AI call ignored its options: %s", out)
	}
}
