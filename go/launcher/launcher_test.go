package launcher

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
)

func put(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0600); err != nil {
		t.Fatal(err)
	}
}
func baseEnv(t *testing.T) []string {
	t.Helper()
	return []string{"TRACEAI_CACHE_DIR=" + filepath.Join(t.TempDir(), "cache"), "PATH=" + os.Getenv("PATH"), "GOMAXPROCS=4", "FI_API_KEY=unit-api-secret", "FI_SECRET_KEY=unit-secret-key"}
}
func TestDetection(t *testing.T) {
	for _, tc := range []struct {
		files []string
		want  []string
	}{
		{nil, nil},
		{[]string{"requirements-dev.txt"}, []string{"python"}},
		{[]string{"pyproject.toml", "package.json", "build.gradle.kts", "App.csproj", "go.mod"}, []string{"python", "node", "java", "dotnet", "go"}},
		{[]string{"pom.xml", "requirements.txt", "package-lock.json"}, []string{"python", "java"}},
	} {
		t.Run(strings.Join(tc.files, "+"), func(t *testing.T) {
			dir := t.TempDir()
			for _, file := range tc.files {
				put(t, filepath.Join(dir, file), "x")
			}
			if got := DetectProject(dir); !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("got %v want %v", got, tc.want)
			}
		})
	}
}
func TestPackageManagers(t *testing.T) {
	for _, manager := range []struct{ file, want string }{{"pnpm-lock.yaml", "pnpm"}, {"yarn.lock", "yarn"}, {"bun.lock", "bun"}, {"package-lock.json", "npm"}} {
		t.Run(manager.want, func(t *testing.T) {
			dir := t.TempDir()
			put(t, filepath.Join(dir, manager.file), "lock")
			if got := packageManager(dir); got != manager.want {
				t.Fatal(got)
			}
		})
	}
}
func TestMergeAndNestedIdempotency(t *testing.T) {
	env := baseEnv(t)
	dir := t.TempDir()
	agentDir := filepath.Join(dir, "agent with spaces")
	jar := filepath.Join(agentDir, "agent.jar")
	dll := filepath.Join(agentDir, "hook.dll")
	put(t, jar, "jar")
	put(t, dll, "dll")
	env = append(env, "PYTHONPATH=/user/a"+string(os.PathListSeparator)+"/user/b", "NODE_OPTIONS=--max-old-space-size=256", "JAVA_TOOL_OPTIONS=-Dfoo=bar", "DOTNET_STARTUP_HOOKS=/existing/hook.dll", "TRACEAI_JAVA_AGENT="+jar, "TRACEAI_DOTNET_AGENT="+dll)
	first := Prepare(nil, env, dir, Options{})
	second := Prepare(nil, first.Environment, dir, Options{})
	if !reflect.DeepEqual(first.Environment, second.Environment) {
		t.Fatalf("nested launch changed environment\n%v\n%v", first.Environment, second.Environment)
	}
	actual := environmentMap(first.Environment)
	for key, want := range map[string]string{"NODE_OPTIONS": "--max-old-space-size=256", "JAVA_TOOL_OPTIONS": "-Dfoo=bar", "PYTHONPATH": "/user/b", "DOTNET_STARTUP_HOOKS": "/existing/hook.dll"} {
		if !strings.Contains(actual[key], want) {
			t.Fatalf("%s clobbered", key)
		}
	}
	words, err := optionWords(actual["JAVA_TOOL_OPTIONS"])
	if err != nil || words[0] != "-javaagent:"+filepath.ToSlash(jar) {
		t.Fatalf("JVM quoting: %v %v", words, err)
	}
	words, err = nodeOptionWords(actual["NODE_OPTIONS"])
	if err != nil || len(words) != 3 || words[0] != "--require" || !strings.Contains(words[1], "preload.cjs") {
		t.Fatalf("Node quoting: %v %v", words, err)
	}
	if actual["TRACEAI_LAUNCHER"] != Version || actual["FI_AUTO_INSTRUMENT"] != "" {
		t.Fatal("missing marker or unsafe .pth activation")
	}
}
func TestOptionWords(t *testing.T) {
	for _, tc := range []struct {
		raw  string
		want []string
		bad  bool
	}{
		{`--require "/path with spaces/preload.cjs" --flag`, []string{"--require", "/path with spaces/preload.cjs", "--flag"}, false},
		{`"-javaagent:/path with spaces/agent.jar" -Dfoo=bar`, []string{"-javaagent:/path with spaces/agent.jar", "-Dfoo=bar"}, false},
		{`--require "broken`, nil, true},
		{`-tags='hello world'`, []string{"-tags=hello world"}, false},
	} {
		t.Run(tc.raw, func(t *testing.T) {
			got, err := optionWords(tc.raw)
			if (err != nil) != tc.bad || !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("%v %v", got, err)
			}
		})
	}
}
func TestConfigFlagMapping(t *testing.T) {
	opts := Options{Project: "project", ProjectType: "experiment", BaseURL: "http://localhost:1234", Disable: "requests", Only: "wire", SampleRate: "0.25", Debug: true}
	plan := Prepare(nil, baseEnv(t), t.TempDir(), opts)
	env := environmentMap(plan.Environment)
	for key, want := range map[string]string{"FI_PROJECT_NAME": "project", "FI_PROJECT_TYPE": "experiment", "FI_BASE_URL": "http://localhost:1234", "FI_DISABLED_INSTRUMENTATIONS": "requests", "FI_INSTRUMENTATIONS": "wire", "FI_SAMPLE_RATE": "0.25", "FI_AUTO_DEBUG": "true", "TRACEAI_DEBUG": "true"} {
		if env[key] != want {
			t.Errorf("%s got %q want %q", key, env[key], want)
		}
	}
}
func TestShimContentAndHash(t *testing.T) {
	plan := Prepare(nil, baseEnv(t), t.TempDir(), Options{})
	env := environmentMap(plan.Environment)
	py := filepath.Join(strings.Split(env["PYTHONPATH"], string(os.PathListSeparator))[0], "sitecustomize.py")
	data, err := os.ReadFile(py)
	if err != nil {
		t.Fatal(err)
	}
	for _, needle := range []string{"fi_instrumentation.auto._bootstrap", "_start()", "PathFinder.find_spec", "except BaseException", "Python lacks fi-instrumentation-otel"} {
		if !strings.Contains(string(data), needle) {
			t.Errorf("missing %s", needle)
		}
	}
	if !strings.Contains(py, hashBytes(data)) {
		t.Fatal("shim is not content-addressed")
	}
	words, _ := nodeOptionWords(env["NODE_OPTIONS"])
	node, err := os.ReadFile(words[1])
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(node), "__TRACEAI_NODE_BUNDLE__") {
		t.Fatal("unexpanded node path")
	}
	for _, needle := range []string{"process.cwd()", "@traceai/auto/register", "try {", "catch (_)", "__traceaiLauncherLoaded"} {
		if !strings.Contains(string(node), needle) {
			t.Errorf("missing %s", needle)
		}
	}
}
func TestCacheAtomicConcurrent(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "cache")
	payload := strings.Repeat("payload", 1024)
	var wg sync.WaitGroup
	errs := make(chan error, 24)
	for i := 0; i < 24; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := installFiles(dir, "node", map[string][]byte{"preload.cjs": []byte(payload), "hook.mjs": []byte("hook")})
			errs <- err
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	agent := discoverAgents(dir, map[string]string{})
	data, err := os.ReadFile(agent.Node)
	if err != nil || string(data) != payload {
		t.Fatalf("partial cache data: %v", err)
	}
	entries, _ := filepath.Glob(filepath.Join(dir, "agents", "node", "*", ".traceai-*"))
	if len(entries) != 0 {
		t.Fatal(entries)
	}
}
func TestCacheRejectsTraversal(t *testing.T) {
	for _, name := range []string{"../escape", "/absolute"} {
		t.Run(name, func(t *testing.T) {
			_, err := installFiles(t.TempDir(), "node", map[string][]byte{"preload.cjs": []byte("ok"), name: []byte("bad")})
			if err == nil {
				t.Fatal("accepted traversal")
			}
		})
	}
}
func TestFailOpenUnwritableCache(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "file")
	put(t, file, "not a directory")
	plan := Prepare(nil, []string{"TRACEAI_CACHE_DIR=" + file, "KEEP=original"}, dir, Options{})
	env := environmentMap(plan.Environment)
	if env["KEEP"] != "original" || len(plan.Warnings) == 0 {
		t.Fatal(plan)
	}
	if env["NODE_OPTIONS"] != "" || env["PYTHONPATH"] != "" {
		t.Fatal("injected unusable shims")
	}
}
func TestRuntimeSelection(t *testing.T) {
	plan := Prepare(nil, baseEnv(t), t.TempDir(), Options{Runtimes: []string{"python"}})
	if _, ok := plan.Changes["NODE_OPTIONS"]; ok {
		t.Fatal("runtime filter ignored")
	}
}
func TestMasking(t *testing.T) {
	actual := Masked(map[string]string{"FI_API_KEY": "do-not-print", "FI_SECRET_KEY": "private-value", "FI_BASE_URL": "https://name:pass@example.com/path?token=sensitive", "FI_PROJECT_NAME": "project-do-not-print", "SOME_TOKEN": "token-value"})
	data, _ := json.Marshal(actual)
	for _, secret := range []string{"do-not-print", "private-value", "name:pass", "sensitive", "token-value"} {
		if strings.Contains(string(data), secret) {
			t.Fatalf("leaked %s", secret)
		}
	}
	if actual["FI_API_KEY"] != "***" {
		t.Fatal(actual)
	}
}
func TestResolveConfig(t *testing.T) {
	dir := t.TempDir()
	put(t, filepath.Join(dir, "futureagi.toml"), "project_name = \"file # project\" #comment\nproject_type = 'experiment'\ninstrumentations = [\"wire\", \"openai\"]\nsample_rate = 0.2\ndebug = true\n[tables]\n")
	config, err := ResolveConfig(dir, []string{"FI_PROJECT_NAME=env", "FI_SAMPLE_RATE=0.75"})
	if err != nil {
		t.Fatal(err)
	}
	for key, want := range map[string]string{"FI_PROJECT_NAME": "env", "FI_PROJECT_TYPE": "experiment", "FI_INSTRUMENTATIONS": "wire,openai", "FI_SAMPLE_RATE": "0.75", "FI_AUTO_DEBUG": "true"} {
		if config[key] != want {
			t.Errorf("%s got %s", key, config[key])
		}
	}
}
func TestInvalidConfig(t *testing.T) {
	for _, tc := range []string{"FI_SAMPLE_RATE=NaN", "FI_SAMPLE_RATE=1.1", "FI_PROJECT_TYPE=invalid", "FI_BASE_URL=ftp://example.com", "FI_BASE_URL=https://user:pass@host", "FI_WIRE_MAX_BODY_BYTES=0", "FI_WIRE_CAPTURE=wat"} {
		t.Run(tc, func(t *testing.T) {
			if _, err := ResolveConfig(t.TempDir(), []string{tc}); err == nil {
				t.Fatal("accepted invalid config")
			}
		})
	}
}
func TestCollectorStatus(t *testing.T) {
	for _, code := range []int{101, 200, 204, 400, 401, 403, 404, 500, 503, 302} {
		t.Run(fmt.Sprint(code), func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				body, _ := io.ReadAll(r.Body)
				if r.URL.Path != "/tracer/v1/traces" || len(body) != 0 || r.Header.Get("X-Api-Key") != "api" || r.Header.Get("X-Secret-Key") != "secret" || r.Header.Get("Content-Type") != "application/x-protobuf" {
					t.Errorf("bad test export: %v", r)
				}
				w.WriteHeader(code)
			}))
			defer server.Close()
			result := CheckCollector(context.Background(), map[string]string{"FI_API_KEY": "api", "FI_SECRET_KEY": "secret", "FI_BASE_URL": server.URL})
			want := code == 200 || code == 204
			if result.OK != want {
				t.Fatalf("%d: %+v", code, result)
			}
		})
	}
}
func TestCollectorMissingKeys(t *testing.T) {
	if result := CheckCollector(context.Background(), map[string]string{}); result.OK {
		t.Fatal(result)
	}
}
func TestGoTargetParsing(t *testing.T) {
	for _, tc := range []struct {
		args []string
		want []string
		bad  bool
	}{
		{[]string{"-tags", "integration", "./cmd/..."}, []string{"./cmd/..."}, false},
		{[]string{"-o", "out", "."}, []string{"."}, false},
		{[]string{"-run", "TestFoo", "./...", "-args", "app"}, []string{"./..."}, false},
		{[]string{"main.go"}, nil, true},
		{[]string{"example.com/cmd@latest"}, nil, true},
	} {
		t.Run(strings.Join(tc.args, " "), func(t *testing.T) {
			got, err := goTargets(tc.args, "build")
			if (err != nil) != tc.bad || !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("%v %v", got, err)
			}
		})
	}
}
func TestExplicitGOWORKPreserved(t *testing.T) {
	dir := t.TempDir()
	env := map[string]string{"GOWORK": "off"}
	warning := prepareGo([]string{"go", "run", "."}, dir, t.TempDir(), "missing", env, func(k, v string) { t.Fatal("must not inject") })
	if warning != "" {
		t.Fatal(warning)
	}
}
func TestInstallFromDirectory(t *testing.T) {
	source := t.TempDir()
	put(t, filepath.Join(source, "preload.cjs"), "bundle")
	put(t, filepath.Join(source, "hook.mjs"), "hook")
	put(t, filepath.Join(source, ".license"), "license")
	env := baseEnv(t)
	if err := Install("node", source, t.TempDir(), env); err != nil {
		t.Fatal(err)
	}
	cache, _ := cacheDirectory(environmentMap(env))
	agent := discoverAgents(cache, environmentMap(env))
	if data, err := os.ReadFile(filepath.Join(filepath.Dir(agent.Node), ".license")); err != nil || string(data) != "license" {
		t.Fatalf("directory copy: %q %v", data, err)
	}
}
