package launcher

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
)

func TestConfigTablesCannotRedirectCollectorCredentials(t *testing.T) {
	for _, header := range []string{"[provider]", "[[provider]]", "[provider.nested]", `["provider"]`} {
		t.Run(header, func(t *testing.T) {
			dir := isolatedCLI(t)
			var collectorCalls, providerCalls atomic.Int32
			collector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				collectorCalls.Add(1)
				if r.URL.Path != "/tracer/v1/traces" || r.Header.Get("X-Api-Key") != "command-api-key" || r.Header.Get("X-Secret-Key") != "command-secret-key" {
					t.Error("collector request changed")
				}
				w.WriteHeader(200)
			}))
			defer collector.Close()
			provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				providerCalls.Add(1)
				w.WriteHeader(200)
			}))
			defer provider.Close()
			t.Setenv("FI_BASE_URL", "")
			if err := os.Unsetenv("FI_BASE_URL"); err != nil {
				t.Fatal(err)
			}
			put(t, filepath.Join(dir, "config.toml"), fmt.Sprintf("base_url = %q\n%s\nbase_url = %q\napi_key = 'provider-key'\n[other]\nproject_type = 'invalid'\n", collector.URL, header, provider.URL))
			code, out, stderr := captureCLI(t, func() int { return Main([]string{"validate", "--json"}) })
			if code != 0 || collectorCalls.Load() != 1 || providerCalls.Load() != 0 {
				t.Fatalf("configuration redirected export: code=%d collector=%d provider=%d %s %s", code, collectorCalls.Load(), providerCalls.Load(), out, stderr)
			}
		})
	}
}

func TestOpaqueRuntimeOptionsNeverAppearInDiagnostics(t *testing.T) {
	dir := isolatedCLI(t)
	secret := "VERIFY_OPTION_SECRET_830193"
	jar := filepath.Join(dir, "agent.jar")
	put(t, jar, "agent")
	t.Setenv("TRACEAI_JAVA_AGENT", jar)
	t.Setenv("JAVA_TOOL_OPTIONS", "-Ddatabase.password="+secret)
	for _, args := range [][]string{{"run", "--dry-run", "--", "true"}, {"env", "--format", "json", "java"}, {"doctor", "--json", "--offline"}} {
		code, out, stderr := captureCLI(t, func() int { return Main(args) })
		if code != 0 || strings.Contains(out+stderr, secret) {
			t.Fatalf("%v exposed opaque option or failed: %d %s %s", args, code, out, stderr)
		}
	}
	for _, key := range []string{"JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "NODE_OPTIONS", "RUBYOPT", "GOFLAGS"} {
		raw := map[string]string{key: "arbitrary=" + secret}
		before := raw[key]
		if Masked(raw)[key] != "***" || raw[key] != before {
			t.Fatal("option changed or exposed", key)
		}
	}
	plan := Prepare(nil, os.Environ(), dir, Options{Runtimes: []string{"java"}})
	if !strings.Contains(environmentMap(plan.Environment)["JAVA_TOOL_OPTIONS"], "-Ddatabase.password="+secret) {
		t.Fatal("masking altered application options")
	}
}

func TestGoModuleGraphComparison(t *testing.T) {
	original := map[string]string{"app": "", "shared": "v1.0.0\x00/local/one\x00"}
	for _, tc := range []struct {
		name  string
		graph map[string]string
		want  bool
	}{
		{"same", map[string]string{"app": "", "shared": "v1.0.0\x00/local/one\x00", "agent-only": "v2"}, true},
		{"upgrade", map[string]string{"app": "", "shared": "v1.1.0\x00/local/one\x00"}, false},
		{"redirect", map[string]string{"app": "", "shared": "v1.0.0\x00/local/two\x00"}, false},
		{"missing", map[string]string{"app": ""}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := preservedGoModules(original, tc.graph); got != tc.want {
				t.Fatal(got)
			}
		})
	}
}

func TestCacheReinstallationRemovesExtraFiles(t *testing.T) {
	for _, concurrent := range []bool{false, true} {
		t.Run(fmt.Sprint(concurrent), func(t *testing.T) {
			cache := filepath.Join(t.TempDir(), "cache")
			files := map[string][]byte{"preload.cjs": []byte("ORIGINAL"), "nested/hook.cjs": []byte("DEPENDENCY")}
			root, err := installFiles(cache, "node", files)
			if err != nil {
				t.Fatal(err)
			}
			put(t, filepath.Join(root, "junk.txt"), "extra")
			put(t, filepath.Join(root, "junk/sub/extra.cjs"), "extra")
			if cachedAgent(cache, "node") != "" {
				t.Fatal("accepted corrupt payload")
			}
			count := 1
			if concurrent {
				count = 8
			}
			start := make(chan struct{})
			errs := make(chan error, count)
			var ready sync.WaitGroup
			ready.Add(count)
			for i := 0; i < count; i++ {
				go func() {
					ready.Done()
					<-start
					got, err := installFiles(cache, "node", files)
					if err == nil && got != root {
						err = fmt.Errorf("payload identity changed")
					}
					errs <- err
				}()
			}
			ready.Wait()
			close(start)
			for i := 0; i < count; i++ {
				if err := <-errs; err != nil {
					t.Fatal(err)
				}
			}
			if !validPayload(root) || cachedAgent(cache, "node") != filepath.Join(root, "preload.cjs") {
				t.Fatal("repair did not restore payload")
			}
			entries, err := os.ReadDir(root)
			if err != nil {
				t.Fatal(err)
			}
			var names []string
			for _, entry := range entries {
				names = append(names, entry.Name())
			}
			if !reflect.DeepEqual(names, []string{"nested", "preload.cjs"}) {
				t.Fatal("extras remain", names)
			}
		})
	}
}
