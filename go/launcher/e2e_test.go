//go:build !windows

package launcher

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/base64"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

// These real-process tests deliberately run serially. All outputs and fixtures
// live below TMPDIR (set to the external volume by make test).
func hostGuard(t *testing.T) {
	t.Helper()
	home, _ := os.UserHomeDir()
	path := filepath.Join(home, ".futureagi-local", "bin", "host-status.sh")
	if _, err := os.Stat(path); err != nil {
		return
	}
	for attempt := 0; attempt < 20; attempt++ {
		data, err := exec.Command(path).CombinedOutput()
		if err != nil {
			t.Fatalf("host status failed: %s", data)
		}
		text := string(data)
		free := regexp.MustCompile(`free=(\d+)%`).FindStringSubmatch(text)
		load := regexp.MustCompile(`load=([0-9.]+)`).FindStringSubmatch(text)
		if len(free) != 2 || len(load) != 2 {
			t.Fatalf("cannot parse host status: %s", text)
		}
		f, _ := strconv.Atoi(free[1])
		l, _ := strconv.ParseFloat(load[1], 64)
		if f >= 20 && l <= 30 {
			return
		}
		t.Logf("waiting for host budget: %s", text)
		<-time.After(60 * time.Second)
	}
	t.Fatal("host remains above budget; no runtime started")
}
func repoRoot(t *testing.T) string {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	return filepath.Clean(filepath.Join(filepath.Dir(file), "../.."))
}

type mockRuntime struct {
	server *httptest.Server
	mu     sync.Mutex
	spans  [][]byte
	calls  int
}

const providerResponse = `{"id":"chatcmpl-launcher","object":"chat.completion","created":1,"model":"launcher-model","choices":[{"index":0,"message":{"role":"assistant","content":"launcher-ok"},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}`

func newMock(t *testing.T) *mockRuntime {
	t.Helper()
	m := &mockRuntime{}
	m.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/chat/completions":
			body, _ := io.ReadAll(io.LimitReader(r.Body, 1<<20))
			if !bytes.Contains(body, []byte("messages")) {
				t.Error("invalid provider request")
			}
			m.mu.Lock()
			m.calls++
			m.mu.Unlock()
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, providerResponse)
		case "/tracer/v1/traces":
			if r.Header.Get("X-Api-Key") != "launcher-api" || r.Header.Get("X-Secret-Key") != "launcher-secret" {
				t.Error("export missing credentials")
			}
			var reader io.Reader = r.Body
			if r.Header.Get("Content-Encoding") == "gzip" {
				gz, err := gzip.NewReader(r.Body)
				if err != nil {
					t.Error(err)
					w.WriteHeader(400)
					return
				}
				defer gz.Close()
				reader = gz
			}
			body, _ := io.ReadAll(io.LimitReader(reader, 4<<20))
			m.mu.Lock()
			m.spans = append(m.spans, body)
			m.mu.Unlock()
			w.Header().Set("Content-Type", "application/x-protobuf")
		default:
			w.WriteHeader(404)
		}
	}))
	t.Cleanup(m.server.Close)
	return m
}
func (m *mockRuntime) assertSpans(t *testing.T, count int) {
	t.Helper()
	deadline := time.Now().Add(8 * time.Second)
	for {
		m.mu.Lock()
		calls := m.calls
		all := bytes.Join(m.spans, nil)
		m.mu.Unlock()
		if calls == count && bytes.Count(all, []byte("traceai.capture.layer")) == count && bytes.Contains(all, []byte("wire")) && bytes.Contains(all, []byte("gen_ai.operation.name")) {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("wanted %d provider calls and wire spans; calls=%d OTLP=%q", count, calls, string(all))
		}
		<-time.After(30 * time.Millisecond)
	}
}
func testEnv(t *testing.T, m *mockRuntime, root string) []string {
	t.Helper()
	env := environmentMap(os.Environ())
	for key := range env {
		if strings.HasPrefix(key, "FI_") || strings.HasPrefix(key, "TRACEAI_") || key == "PYTHONPATH" || key == "NODE_OPTIONS" || key == "JAVA_TOOL_OPTIONS" || key == "DOTNET_STARTUP_HOOKS" || key == "GOWORK" || key == "RUBYOPT" || key == "RUBYLIB" {
			delete(env, key)
		}
	}
	for key, value := range map[string]string{"TRACEAI_CACHE_DIR": filepath.Join(t.TempDir(), "cache with space"), "FI_API_KEY": "launcher-api", "FI_SECRET_KEY": "launcher-secret", "FI_BASE_URL": m.server.URL, "FI_PROJECT_NAME": "launcher-e2e", "FI_INSTRUMENTATIONS": "wire", "TRACEAI_JAVA_AGENT": filepath.Join(root, "java/traceai-javaagent/target/traceai-javaagent.jar"), "TRACEAI_DOTNET_AGENT": filepath.Join(root, "csharp/src/TraceAI.AutoInstrumentation/bin/Release/net8.0/TraceAI.AutoInstrumentation.dll"), "TRACEAI_GO_AGENT": filepath.Join(root, "go/auto"), "TRACEAI_NODE_AGENT": filepath.Join(root, "typescript/packages/traceai_auto/dist/src/register.js")} {
		env[key] = value
	}
	env["JAVA_HOME"] = "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home"
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return environmentList(env)
}
func launch(t *testing.T, binary, dir string, env []string, args ...string) string {
	t.Helper()
	hostGuard(t)
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, binary, append([]string{"run", "--"}, args...)...)
	cmd.Dir = dir
	cmd.Env = env
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("%v failed: %v\n%s", args, err, output)
	}
	return string(output)
}
func requireProgram(t *testing.T, path string) string {
	t.Helper()
	resolved, err := exec.LookPath(path)
	if err != nil {
		t.Skipf("runtime absent: %s", path)
	}
	return resolved
}
func TestE2E(t *testing.T) {
	if os.Getenv("TRACEAI_E2E") != "1" {
		t.Skip("set TRACEAI_E2E=1 for serialized real-runtime tests")
	}
	root := repoRoot(t)
	module := filepath.Join(root, "go/launcher")
	binary := filepath.Join(t.TempDir(), "traceai")
	hostGuard(t)
	build := exec.Command("go", "build", "-tags", "embedagents", "-o", binary, "./cmd/traceai")
	build.Dir = module
	build.Env = os.Environ()
	if output, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build launcher: %v\n%s", err, output)
	}
	python := "/tmp/fi-auto-scratch/real313/bin/python"
	node := func(t *testing.T) string { return requireProgram(t, "node") }
	pythonScript := `import os, requests
r = requests.post(os.environ['FI_BASE_URL'] + '/v1/chat/completions', json={'model':'launcher-model','messages':[{'role':'user','content':'hello'}]})
assert r.status_code == 200
assert r.json()['choices'][0]['message']['content'] == 'launcher-ok'
print('PYTHON_OK')
`
	nodeScript := `(async function(){var r=await fetch(process.env.FI_BASE_URL+'/v1/chat/completions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:'launcher-model',messages:[{role:'user',content:'hello'}]})});var result=await r.json();if(result.choices[0].message.content!=='launcher-ok')throw new Error('changed response');console.log('NODE_OK');})().catch(function(e){console.error(e);process.exitCode=1;});`
	t.Run("python_requests", func(t *testing.T) {
		requireProgram(t, python)
		m := newMock(t)
		dir := t.TempDir()
		put(t, filepath.Join(dir, "app.py"), pythonScript)
		output := launch(t, binary, dir, testEnv(t, m, root), python, "app.py")
		if !strings.Contains(output, "PYTHON_OK") {
			t.Fatal(output)
		}
		m.assertSpans(t, 1)
	})
	t.Run("node_fetch_project_install", func(t *testing.T) {
		runtime := node(t)
		agent := filepath.Join(root, "typescript/packages/traceai_auto")
		if !readableFile(filepath.Join(agent, "dist/src/register.js")) {
			t.Skip("built Node agent absent")
		}
		m := newMock(t)
		dir := t.TempDir()
		put(t, filepath.Join(dir, "app.cjs"), nodeScript)
		if err := os.MkdirAll(filepath.Join(dir, "node_modules/@traceai"), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink(agent, filepath.Join(dir, "node_modules/@traceai/auto")); err != nil {
			t.Fatal(err)
		}
		env := environmentMap(testEnv(t, m, root))
		env["TRACEAI_NODE_AGENT"] = "/missing-fallback"
		output := launch(t, binary, dir, environmentList(env), runtime, "app.cjs")
		if !strings.Contains(output, "NODE_OK") {
			t.Fatal(output)
		}
		m.assertSpans(t, 1)
	})
	t.Run("java_jar_agent_path_with_space", func(t *testing.T) {
		java := requireProgram(t, "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home/bin/java")
		jars, _ := filepath.Glob(filepath.Join(root, "java/traceai-javaagent/target/plain-app-*.jar"))
		if len(jars) == 0 {
			t.Skip("existing executable app.jar absent; do not rebuild JVM agent")
		}
		agent := filepath.Join(root, "java/traceai-javaagent/target/traceai-javaagent.jar")
		if !readableFile(agent) {
			t.Skip("existing Java agent absent")
		}
		dir := t.TempDir()
		spaced := filepath.Join(dir, "space in path", "agent.jar")
		data, err := os.ReadFile(agent)
		if err != nil {
			t.Fatal(err)
		}
		put(t, spaced, string(data))
		m := newMock(t)
		env := environmentMap(testEnv(t, m, root))
		env["TRACEAI_JAVA_AGENT"] = spaced
		output := launch(t, binary, dir, environmentList(env), java, "-jar", jars[0], m.server.URL+"/v1/chat/completions")
		encoded := base64.StdEncoding.EncodeToString([]byte(providerResponse))
		if !strings.Contains(output, encoded) {
			t.Fatal("response changed", output)
		}
		m.assertSpans(t, 1)
	})
	t.Run("java_credential_agent_paths", func(t *testing.T) { checkJavaCredentialPaths(t, binary) })
	for _, tfm := range []string{"net8.0", "net10.0"} {
		t.Run("dotnet_startup_hook_"+tfm, func(t *testing.T) {
			dotnet := requireProgram(t, filepath.Join(root, "csharp/.dotnet/dotnet"))
			app := filepath.Join(root, "csharp/tests/TraceAI.AutoInstrumentation.TestApp/bin/Release", tfm, "TraceAI.AutoInstrumentation.TestApp.dll")
			if !readableFile(app) {
				t.Fatal("required prebuilt .NET TestApp absent; do not rebuild SDK agents", tfm)
			}
			m := newMock(t)
			env := environmentMap(testEnv(t, m, root))
			env["DOTNET_ROOT"] = filepath.Join(root, "csharp/.dotnet")
			env["FI_AUTO_DEBUG"] = "true"
			delete(env, "TRACEAI_DOTNET_AGENT") // The same embedded net8 hook must export on both runtimes.
			output := launch(t, binary, t.TempDir(), environmentList(env), dotnet, app, m.server.URL+"/v1/chat/completions")
			t.Logf(".NET %s runtime output: %s", tfm, output)
			if !strings.Contains(output, base64.StdEncoding.EncodeToString([]byte(providerResponse))) {
				t.Fatal("response changed", output)
			}
			m.assertSpans(t, 1)
		})
	}
	t.Run("go_run_no_project_edits", func(t *testing.T) {
		requireProgram(t, "go")
		m := newMock(t)
		dir := t.TempDir()
		mod := "module example.com/launcher-tiny\n\ngo 1.26.0\n"
		sum := ""
		put(t, filepath.Join(dir, "go.mod"), mod)
		put(t, filepath.Join(dir, "go.sum"), sum)
		put(t, filepath.Join(dir, "main.go"), `package main
import("bytes";"fmt";"io";"net/http";"os";"time")
func main(){r,e:=http.Post(os.Getenv("FI_BASE_URL")+"/v1/chat/completions","application/json",bytes.NewBufferString("{\"model\":\"launcher-model\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}]}"));if e!=nil{panic(e)};b,e:=io.ReadAll(r.Body);r.Body.Close();if e!=nil{panic(e)};fmt.Println(string(b));time.Sleep(700*time.Millisecond)}
`)
		env := environmentMap(testEnv(t, m, root))
		delete(env, "TRACEAI_GO_AGENT") // Exercise the full binary's staged source.
		env["FI_AUTO_DEBUG"] = "true"
		env["TRACEAI_GO_INJECT"] = "1" // Experimental build injection is never implicit.
		output := launch(t, binary, dir, environmentList(env), "go", "run", ".")
		t.Logf("Go runtime output: %s", output)
		if !strings.Contains(output, "launcher-ok") {
			t.Fatal(output)
		}
		m.assertSpans(t, 1)
		for name, want := range map[string]string{"go.mod": mod, "go.sum": sum} {
			got, _ := os.ReadFile(filepath.Join(dir, name))
			if string(got) != want {
				t.Fatalf("%s modified", name)
			}
		}
		matches, _ := filepath.Glob(filepath.Join(dir, "*traceai*"))
		if len(matches) != 0 {
			t.Fatal("project overlay file leaked")
		}
	})
	t.Run("go_default_commands_and_binary_detection", func(t *testing.T) {
		goTool := requireProgram(t, "go")
		dir := t.TempDir()
		mod := "module example.com/go-default\n\ngo 1.26.0\n"
		put(t, filepath.Join(dir, "go.mod"), mod)
		put(t, filepath.Join(dir, "main.go"), "package main\nimport \"fmt\"\nvar value=\"APP_DEFAULT\"\nfunc main(){fmt.Println(value)}\n")
		put(t, filepath.Join(dir, "main_test.go"), "package main\nimport \"testing\"\nfunc TestApp(t *testing.T){}\n")
		m := newMock(t)
		env := environmentMap(testEnv(t, m, root))
		env["GOFLAGS"], env["GOWORK"], env["GOPROXY"] = "-p=2 -ldflags=-X=main.value=APP_UNCHANGED", "off", "off"
		env["GOBIN"] = filepath.Join(dir, "installed")
		for _, sub := range []string{"run", "build", "test", "install"} {
			t.Run(sub, func(t *testing.T) {
				args := []string{sub, "."}
				if sub == "build" {
					args = []string{sub, "-o", filepath.Join(dir, "app"), "."}
				} else if sub == "test" {
					args = []string{sub, "-count=1", "-v", "."}
				}
				code, output, stderr := startupProbe(t, goTool, dir, environmentList(env), args...)
				actualCode, actualOut, actualErr := startupProbe(t, binary, dir, environmentList(env), append([]string{"run", "--", goTool}, args...)...)
				if sub == "test" {
					// Native test-run durations are nondeterministic, not application behavior.
					duration := regexp.MustCompile(`[0-9]+\.[0-9]+s`)
					output, actualOut = duration.ReplaceAllString(output, "TIME"), duration.ReplaceAllString(actualOut, "TIME")
				}
				if code != 0 || actualCode != code || actualOut != output || strings.Count(actualErr, "traceai setup go") != 1 || stderr != "" {
					t.Fatal("default Go command differs from native", sub, code, actualCode)
				}
			})
		}
		app := filepath.Join(dir, "app")
		code, output, stderr := startupProbe(t, binary, dir, environmentList(env), "run", "--", app)
		if code != 0 || output != "APP_UNCHANGED\n" || strings.Count(stderr, "traceai setup go") != 1 {
			t.Fatal("Go binary detection changed application behavior")
		}
		code, output, stderr = startupProbe(t, binary, dir, environmentList(env), "run", "--", binary, "run", "--", app)
		if code != 0 || output != "APP_UNCHANGED\n" || strings.Count(stderr, "traceai setup go") != 1 {
			t.Fatal("nested Go binary detection repeated hint")
		}
		agent := t.TempDir()
		put(t, filepath.Join(agent, "go.mod"), "module "+agentModule+"\n\ngo 1.26.0\n")
		put(t, filepath.Join(agent, "auto.go"), "package auto\nimport \"fmt\"\nfunc init(){fmt.Println(\"AGENT_INIT\")}\n")
		env["TRACEAI_GO_AGENT"], env["GOWORK"] = agent, ""
		code, output, stderr = startupProbe(t, binary, dir, environmentList(env), "run", "--go-inject", "--", goTool, "build", "-o", app, ".")
		if code != 0 || output != "" || stderr != "" {
			t.Fatal("explicit --go-inject failed", code, stderr)
		}
		code, output, stderr = startupProbe(t, binary, dir, environmentList(env), "run", "--", app)
		if code != 0 || output != "AGENT_INIT\nAPP_UNCHANGED\n" || stderr != "" {
			t.Fatal("already-linked Go agent not detected", code)
		}
		data, _ := os.ReadFile(filepath.Join(dir, "go.mod"))
		if string(data) != mod {
			t.Fatal("default or opt-in Go command modified go.mod")
		}
	})
	t.Run("node_fetch_embedded_bundle", func(t *testing.T) {
		runtime := node(t)
		if !readableFile(filepath.Join(module, "embedded/node/preload.cjs")) {
			t.Skip("prebuilt Node bundle absent")
		}
		m := newMock(t)
		dir := t.TempDir()
		put(t, filepath.Join(dir, "app.cjs"), nodeScript)
		env := environmentMap(testEnv(t, m, root))
		delete(env, "TRACEAI_NODE_AGENT")
		output := launch(t, binary, dir, environmentList(env), runtime, "app.cjs")
		if !strings.Contains(output, "NODE_OK") {
			t.Fatal(output)
		}
		m.assertSpans(t, 1)
	})
	t.Run("ruby_net_http_fallback", func(t *testing.T) {
		ruby := os.Getenv("TRACEAI_TEST_RUBY")
		if ruby == "" {
			ruby = "/opt/homebrew/opt/ruby@3.2/bin/ruby"
		}
		requireProgram(t, ruby)
		lib := filepath.Join(root, "ruby/lib")
		if !readableFile(filepath.Join(lib, "traceai/auto.rb")) {
			t.Skip("Ruby agent entry point absent; shim remains fail-open")
		}
		m := newMock(t)
		dir := t.TempDir()
		env := environmentMap(testEnv(t, m, root))
		delete(env, "TRACEAI_RUBY_AGENT") // Exercise the full binary's cached Ruby lib.
		env["FI_AUTO_DEBUG"] = "true"
		vendor := filepath.Join(root, "ruby/.bundle/vendor/ruby/3.2.0")
		if info, err := os.Stat(vendor); err == nil && info.IsDir() {
			env["GEM_HOME"], env["GEM_PATH"] = vendor, vendor
		}
		// Ruby defers activation until the first actual client request, leaving
		// application gem selection untouched. Probe only dependency availability;
		// the request and collector assertion below verify zero-code activation.
		hostGuard(t)
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		probe := exec.CommandContext(ctx, ruby, "-I"+lib, "-rtraceai/auto", "-e", "require 'opentelemetry/sdk'; require 'opentelemetry/exporter/otlp'")
		probe.Dir, probe.Env = dir, environmentList(env)
		if output, err := probe.CombinedOutput(); err != nil {
			t.Skipf("Ruby agent unavailable/incomplete (not a launcher failure): %v %s", err, output)
		}
		script := `require 'net/http'; require 'json'; uri=URI(ENV.fetch('FI_BASE_URL')+'/v1/chat/completions'); req=Net::HTTP::Post.new(uri); req['Content-Type']='application/json'; req.body=JSON.generate(model:'launcher-model',messages:[{role:'user',content:'hello'}]); result=Net::HTTP.start(uri.host,uri.port){|h|h.request(req)}; raise 'changed response' unless JSON.parse(result.body)['choices'][0]['message']['content']=='launcher-ok'; puts 'RUBY_OK'`
		output := launch(t, binary, dir, environmentList(env), ruby, "-e", script)
		t.Logf("Ruby runtime output: %s", output)
		if !strings.Contains(output, "RUBY_OK") {
			t.Fatal(output)
		}
		m.assertSpans(t, 1)
	})
	t.Run("nested_injection_once", func(t *testing.T) {
		m := newMock(t)
		output := launch(t, binary, t.TempDir(), testEnv(t, m, root), binary, "run", "--", "sh", "-c", `printf '%s\n%s' "$NODE_OPTIONS" "$RUBYOPT"`)
		if strings.Count(output, "--require") != 1 || strings.Count(output, "-rtraceai_launcher_") != 1 {
			t.Fatalf("duplicate injection: %s", output)
		}
	})
	t.Run("shell_wrapper_python_and_node", func(t *testing.T) {
		requireProgram(t, python)
		runtime := node(t)
		m := newMock(t)
		dir := t.TempDir()
		put(t, filepath.Join(dir, "app.py"), pythonScript)
		put(t, filepath.Join(dir, "app.cjs"), nodeScript)
		output := launch(t, binary, dir, testEnv(t, m, root), "sh", "-c", shellQuote(python)+" app.py && "+shellQuote(runtime)+" app.cjs")
		if !strings.Contains(output, "PYTHON_OK") || !strings.Contains(output, "NODE_OK") {
			t.Fatal(output)
		}
		m.assertSpans(t, 2)
	})
	t.Run("streams_arguments_environment_cwd", func(t *testing.T) {
		hostGuard(t)
		m := newMock(t)
		dir := t.TempDir()
		env := append(testEnv(t, m, root), "KEEP_APPLICATION_VALUE=unchanged")
		argument := "space ; $(not-a-command) 'quoted'"
		cmd := exec.Command(binary, "run", "--", "sh", "-c", `IFS= read -r line; printf '%s|%s|%s|%s' "$line" "$1" "$KEEP_APPLICATION_VALUE" "$PWD"; printf 'stderr-kept' >&2`, "application", argument)
		cmd.Dir, cmd.Env = dir, env
		cmd.Stdin = strings.NewReader("stdin-kept\n")
		var stdout, stderr bytes.Buffer
		cmd.Stdout, cmd.Stderr = &stdout, &stderr
		if err := cmd.Run(); err != nil {
			t.Fatal(err)
		}
		if stdout.String() != "stdin-kept|"+argument+"|unchanged|"+dir || stderr.String() != "stderr-kept" {
			t.Fatalf("application IO changed: stdout=%q stderr=%q", stdout.String(), stderr.String())
		}
	})
	t.Run("missing_agents_keys_exit3", func(t *testing.T) {
		hostGuard(t)
		m := newMock(t)
		env := environmentMap(testEnv(t, m, root))
		delete(env, "FI_API_KEY")
		delete(env, "FI_SECRET_KEY")
		for _, name := range []string{"JAVA", "DOTNET", "NODE", "GO"} {
			env["TRACEAI_"+name+"_AGENT"] = "/not-installed"
		}
		cmd := exec.Command(binary, "run", "--", "sh", "-c", "printf untouched; exit 3")
		cmd.Env = environmentList(env)
		cmd.Dir = t.TempDir()
		output, err := cmd.CombinedOutput()
		exit, ok := err.(*exec.ExitError)
		if !ok || exit.ExitCode() != 3 || string(output) != "untouched" {
			t.Fatal("program behavior changed", string(output), err)
		}
	})
	t.Run("missing_program_127", func(t *testing.T) {
		hostGuard(t)
		cmd := exec.Command(binary, "run", "--", "traceai-command-does-not-exist-9123")
		cmd.Env = append(os.Environ(), "TRACEAI_CACHE_DIR="+t.TempDir())
		output, err := cmd.CombinedOutput()
		exit, ok := err.(*exec.ExitError)
		if !ok || exit.ExitCode() != 127 || !strings.Contains(string(output), "command not found") {
			t.Fatal(string(output), err)
		}
	})
	t.Run("not_executable_126", func(t *testing.T) {
		hostGuard(t)
		dir := t.TempDir()
		file := filepath.Join(dir, "no-exec")
		put(t, file, "#!/bin/sh\nexit 0\n")
		cmd := exec.Command(binary, "run", "--", file)
		cmd.Env = append(os.Environ(), "TRACEAI_CACHE_DIR="+t.TempDir())
		_, err := cmd.CombinedOutput()
		exit, ok := err.(*exec.ExitError)
		if !ok || exit.ExitCode() != 126 {
			t.Fatal(err)
		}
	})
	t.Run("sigterm_exec_identity", func(t *testing.T) {
		hostGuard(t)
		dir := t.TempDir()
		ready := filepath.Join(dir, "ready")
		cmd := exec.Command(binary, "run", "--", "sh", "-c", "printf ready > "+shellQuote(ready)+"; exec sleep 30")
		cmd.Env = append(os.Environ(), "TRACEAI_CACHE_DIR="+filepath.Join(dir, "cache"))
		if err := cmd.Start(); err != nil {
			t.Fatal(err)
		}
		defer cmd.Process.Kill()
		deadline := time.Now().Add(5 * time.Second)
		for !readableFile(ready) {
			if time.Now().After(deadline) {
				t.Fatal("child did not start")
			}
			<-time.After(10 * time.Millisecond)
		}
		if err := cmd.Process.Signal(syscall.SIGTERM); err != nil {
			t.Fatal(err)
		}
		err := cmd.Wait()
		exit, ok := err.(*exec.ExitError)
		if !ok {
			t.Fatal("SIGTERM lost", err)
		}
		status, ok := exit.Sys().(syscall.WaitStatus)
		if !ok || !status.Signaled() || status.Signal() != syscall.SIGTERM {
			t.Fatal("signal status changed", exit.Sys())
		}
	})
	t.Run("python_sitecustomize_chain", func(t *testing.T) {
		requireProgram(t, python)
		dir := t.TempDir()
		put(t, filepath.Join(dir, "user-site", "sitecustomize.py"), "import os\nos.environ['LAUNCHER_SITE_CHAINED']='yes'\nVALUE='application-helper'\nimport sitecustomize\nassert sitecustomize.VALUE == VALUE\n")
		m := newMock(t)
		env := environmentMap(testEnv(t, m, root))
		env["PYTHONPATH"] = filepath.Join(dir, "user-site")
		env["OTEL_SDK_DISABLED"] = "true"
		output := launch(t, binary, dir, environmentList(env), python, "-c", "import os, sitecustomize; assert os.environ.get('LAUNCHER_SITE_CHAINED')=='yes'; assert sitecustomize.VALUE=='application-helper'; print('CHAINED')")
		if !strings.Contains(output, "CHAINED") {
			t.Fatal(output)
		}
	})
	t.Run("dry_run_masks_secrets", func(t *testing.T) {
		hostGuard(t)
		m := newMock(t)
		env := testEnv(t, m, root)
		cmd := exec.Command(binary, "run", "--dry-run", "--project", "launcher-api", "--", "sh", "-c", "exit 99")
		cmd.Env = env
		cmd.Dir = t.TempDir()
		data, err := cmd.CombinedOutput()
		if err != nil || strings.Contains(string(data), "launcher-api") || strings.Contains(string(data), "launcher-secret") {
			t.Fatal("dry run leaked or executed", string(data), err)
		}
	})
}
