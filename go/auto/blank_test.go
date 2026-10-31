package auto_test

import (
	"bytes"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/future-agi/traceAI/go/auto"
	"github.com/future-agi/traceAI/go/auto/internal/testhost"
	collector "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/protobuf/proto"
)

func cleanEnv() []string {
	out := []string{}
	for _, v := range os.Environ() {
		if strings.HasPrefix(v, "FI_") || strings.HasPrefix(v, "OTEL_") {
			continue
		}
		out = append(out, v)
	}
	return append(out, "GOTOOLCHAIN=auto")
}
func hostReady(t *testing.T) {
	t.Helper()
	home, err := os.UserHomeDir()
	if err != nil {
		t.Fatal(err)
	}
	script := filepath.Join(home, ".futureagi-local/bin/host-status.sh")
	if _, err := os.Stat(script); os.IsNotExist(err) {
		return // The shared-laptop safety helper is not required on other hosts.
	}
	for {
		out, err := exec.Command(script).CombinedOutput()
		if err != nil {
			t.Fatalf("host safety check: %v %s", err, out)
		}
		free, load := -1.0, -1.0
		for _, field := range strings.Fields(string(out)) {
			if strings.HasPrefix(field, "free=") {
				free, _ = strconv.ParseFloat(strings.TrimSuffix(strings.TrimPrefix(field, "free="), "%"), 64)
			}
			if strings.HasPrefix(field, "load=") {
				load, _ = strconv.ParseFloat(strings.TrimPrefix(field, "load="), 64)
			}
		}
		if free < 0 || load < 0 {
			t.Fatalf("cannot parse host status: %s", out)
		}
		if free >= 20 && load <= 30 {
			return
		}
		t.Logf("waiting for host: %s", out)
		time.Sleep(time.Minute)
	}
}

// Child builds/runtimes remain inside the suite's host gate; standalone runs
// acquire their own slot. Never recursively acquire a non-reentrant host slot.
func hostCommand(name string, args ...string) *exec.Cmd {
	return testhost.Command(name, args...)
}

func TestBlankImportE2E(t *testing.T) {
	hostReady(t)
	bin := filepath.Join(t.TempDir(), "plain")
	build := hostCommand("go", "build", "-o", bin, "./testdata/blank")
	build.Env = cleanEnv()
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build plain app %v %s", err, out)
	}
	for _, mode := range []string{"json", "stream", "missing_credentials", "disabled", "sample_zero", "collector_rejected"} {
		t.Run(mode, func(t *testing.T) {
			body := responseJSON
			ct := "application/json"
			if mode == "stream" {
				ct = "text/event-stream"
				body = "data: {\"model\":\"test-model\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"Hello\"}}]}\n\ndata: [DONE]\n\n"
			}
			provider := localServer(t, body, ct, 200)
			var mu sync.Mutex
			names := []string{}
			exports := 0
			sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				mu.Lock()
				defer mu.Unlock()
				exports++
				if r.URL.Path != "/tracer/v1/traces" || r.Header.Get("X-Api-Key") != "test-fi-key" || r.Header.Get("X-Secret-Key") != "test-fi-secret" {
					t.Errorf("invalid exporter endpoint/auth")
				}
				data, _ := io.ReadAll(r.Body)
				var request collector.ExportTraceServiceRequest
				if err := proto.Unmarshal(data, &request); err != nil {
					t.Error(err)
				}
				for _, rs := range request.ResourceSpans {
					for _, ss := range rs.ScopeSpans {
						for _, s := range ss.Spans {
							names = append(names, s.Name)
							content := false
							for _, a := range s.Attributes {
								if a.Key == "output.value" && a.Value.GetStringValue() == "Hello" {
									content = true
								}
							}
							if !content {
								t.Errorf("wire span missing output")
							}
						}
					}
					resources := map[string]string{}
					for _, a := range rs.Resource.Attributes {
						resources[a.Key] = a.Value.GetStringValue()
					}
					if resources["project_type"] != "observe" || resources["project_name"] != "blank-test" {
						t.Errorf("wrong project resource %v", resources)
					}
				}
				w.Header().Set("Content-Type", "application/x-protobuf")
				if mode == "collector_rejected" {
					w.WriteHeader(401)
				} else {
					w.WriteHeader(200)
				}
			}))
			defer sink.Close()
			cmd := hostCommand(bin, provider.URL)
			cmd.Dir = t.TempDir()
			cmd.Env = append(cleanEnv(), "FI_BASE_URL="+sink.URL, "FI_PROJECT_NAME=blank-test")
			if mode != "missing_credentials" {
				cmd.Env = append(cmd.Env, "FI_API_KEY=test-fi-key", "FI_SECRET_KEY=test-fi-secret")
			}
			if mode == "disabled" {
				cmd.Env = append(cmd.Env, "OTEL_SDK_DISABLED=true")
			}
			if mode == "sample_zero" {
				cmd.Env = append(cmd.Env, "FI_SAMPLE_RATE=0")
			}
			var stdout, stderr bytes.Buffer
			cmd.Stdout = &stdout
			cmd.Stderr = &stderr
			if err := cmd.Run(); err != nil {
				t.Fatalf("plain app crashed %v %s", err, stderr.String())
			}
			if stdout.String() != body+body {
				t.Fatal("application bytes changed")
			}
			mu.Lock()
			defer mu.Unlock()
			// S10: an active agent with its own batching exporter prints the
			// one-time Shutdown hint exactly once; everything else stays silent.
			hints := strings.Count(stderr.String(), auto.ShutdownHint)
			if active := mode != "disabled" && mode != "missing_credentials"; hints != map[bool]int{true: 1, false: 0}[active] {
				t.Fatalf("want exactly one startup hint when active, got %d: %q", hints, stderr.String())
			}
			warnings := strings.Count(strings.ReplaceAll(stderr.String(), auto.ShutdownHint, ""), "traceai auto:")
			if mode == "disabled" || mode == "sample_zero" {
				if exports != 0 || warnings != 0 {
					t.Fatalf("disabled/unsampled agent exported or warned: %d %s", exports, stderr.String())
				}
				return
			}
			if mode == "collector_rejected" && warnings != 1 {
				t.Fatalf("rejected collector did not fail open with one warning: %q", stderr.String())
			}
			if mode == "missing_credentials" {
				if exports != 0 {
					t.Fatal("export with missing keys")
				}
				if warnings != 1 {
					t.Fatalf("want one warning got %q", stderr.String())
				}
			} else {
				if len(names) != 2 {
					t.Fatalf("want two exported spans got %v stderr %s", names, stderr.String())
				}
				for _, name := range names {
					if name != "chat test-model" {
						t.Fatalf("wrong wire span %s", name)
					}
				}
			}
		})
	}
}

// S10 differential (2026-10-31): a program that returns from main right after
// its AI call. With the documented auto.Shutdown the span is delivered every
// time; without it, delivery is not guaranteed (Go has no exit hook) and the
// one-time startup hint tells the user so. Application bytes never change.
func TestShutdownBeforeMainReturnsE2E(t *testing.T) {
	hostReady(t)
	bin := filepath.Join(t.TempDir(), "shutdown")
	build := hostCommand("go", "build", "-o", bin, "./testdata/shutdown")
	build.Env = cleanEnv()
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build shutdown app %v %s", err, out)
	}
	for _, mode := range []string{"shutdown", "immediate"} {
		t.Run(mode, func(t *testing.T) {
			provider := localServer(t, responseJSON, "application/json", 200)
			for run := 0; run < 3; run++ {
				var mu sync.Mutex
				spans := 0
				sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					data, _ := io.ReadAll(r.Body)
					var request collector.ExportTraceServiceRequest
					if err := proto.Unmarshal(data, &request); err != nil {
						t.Error(err)
					}
					mu.Lock()
					for _, rs := range request.ResourceSpans {
						for _, ss := range rs.ScopeSpans {
							spans += len(ss.Spans)
						}
					}
					mu.Unlock()
					w.Header().Set("Content-Type", "application/x-protobuf")
				}))
				cmd := hostCommand(bin, provider.URL, mode)
				cmd.Dir = t.TempDir()
				cmd.Env = append(cleanEnv(), "FI_BASE_URL="+sink.URL, "FI_API_KEY=test-fi-key", "FI_SECRET_KEY=test-fi-secret")
				var stdout, stderr bytes.Buffer
				cmd.Stdout, cmd.Stderr = &stdout, &stderr
				if err := cmd.Run(); err != nil {
					t.Fatalf("short-lived app failed %v %s", err, stderr.String())
				}
				sink.Close()
				if stdout.String() != responseJSON || strings.Count(stderr.String(), auto.ShutdownHint) != 1 || strings.Count(stderr.String(), "traceai auto:") != 1 {
					t.Fatalf("application bytes or startup hint changed: %q %q", stdout.String(), stderr.String())
				}
				mu.Lock()
				delivered := spans
				mu.Unlock()
				if mode == "shutdown" && delivered != 1 {
					t.Fatalf("auto.Shutdown before main returned delivered %d spans, want 1", delivered)
				}
				if mode == "immediate" {
					t.Logf("run %d without Shutdown delivered %d spans (not guaranteed; documented)", run, delivered)
				}
			}
		})
	}
}
