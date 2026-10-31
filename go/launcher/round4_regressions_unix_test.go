//go:build !windows

package launcher

import (
	"archive/zip"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"syscall"
	"testing"
	"time"
)

// Value-free diagnostics are intentional: env contains the whole CI environment,
// and even a probe warning may be untrusted. Exercise the actual failure path.
func workspacePreservationFailure(warning string, original []string, env map[string]string) string {
	if !strings.Contains(warning, "dependency selections") || !reflect.DeepEqual(original, environmentList(env)) {
		return "dependency upgrade was activated or could not be checked"
	}
	return ""
}

func TestWorkspaceFailureDiagnosticsContainNoEnvironmentValues(t *testing.T) {
	secret := "VERIFY_DUMMY_CREDENTIAL_329580"
	env := map[string]string{"FI_API_KEY": secret, "OTHER_APPLICATION_VALUE": "private-application-data"}
	original := environmentList(env)
	for _, warning := range []string{"", secret} {
		failure := workspacePreservationFailure(warning, original, env)
		if failure == "" || strings.Contains(failure, secret) || strings.Contains(failure, "private-application-data") || strings.Contains(failure, "FI_API_KEY") {
			t.Fatal("regression failure diagnostics expose environment values")
		}
	}
	env["GOWORK"] = "private-workspace-path"
	if workspacePreservationFailure("dependency selections", original, env) == "" {
		t.Fatal("changed environment accepted")
	}
}

func TestExplicitEmptyPathSearchesWorkingDirectory(t *testing.T) {
	dir := t.TempDir()
	program := filepath.Join(dir, "localtool")
	executable(t, program, "printf LOCAL_OK")
	found, err := lookProgram("localtool", dir, map[string]string{"PATH": ""})
	if err != nil || found != program {
		t.Fatal("empty PATH lost working-directory lookup", err)
	}
	if _, err := lookProgram("missing-localtool", dir, map[string]string{"PATH": ""}); err == nil {
		t.Fatal("nonexistent program found")
	}
	if err := os.Chmod(program, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := lookProgram("localtool", dir, map[string]string{"PATH": ""}); err != errNotExecutable {
		t.Fatal("nonexecutable PATH match lost status", err)
	}
}

func TestLocalAgentFIFORejectedBeforeOpen(t *testing.T) {
	if pipe := os.Getenv("TRACEAI_TEST_FIFO_INSTALL"); pipe != "" {
		if err := Install("java", pipe, filepath.Dir(pipe), os.Environ()); err == nil {
			t.Fatal("single-file FIFO accepted")
		}
		return
	}
	dir := t.TempDir()
	pipe := filepath.Join(dir, "payload.pipe")
	if err := syscall.Mkfifo(pipe, 0600); err != nil {
		t.Fatal(err)
	}
	// Use a guarded ReadFileFS to catch attempted reads synchronously without
	// blocking even when this regression is reintroduced.
	source := &specialFileFS{FS: os.DirFS(dir)}
	if _, err := filesFromFS(source, "."); err == nil || source.reads != 0 {
		t.Fatal("FIFO was read or accepted")
	}
	// Isolate the single-file path so a regression in its pre-open check is
	// bounded too. Process termination, not a timer/sleep, is the success barrier.
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestLocalAgentFIFORejectedBeforeOpen$")
	cmd.Env = []string{"TRACEAI_TEST_FIFO_INSTALL=" + pipe, "TRACEAI_CACHE_DIR=" + filepath.Join(dir, "cache")}
	if err := cmd.Run(); err != nil {
		t.Fatal("single-file FIFO was accepted or blocked installation")
	}
}

func TestGeneratedShEnvironmentPreservesRuntimeOptions(t *testing.T) {
	dir := isolatedCLI(t)
	jar := filepath.Join(dir, "agent.jar")
	put(t, jar, "agent")
	t.Setenv("TRACEAI_JAVA_AGENT", jar)
	inherited := map[string]string{
		"JAVA_TOOL_OPTIONS": `-Xmx128m -Dpassword=VERIFY_OPTION_SECRET_830193`,
		"NODE_OPTIONS":      `--title="literal $dollar 'quote' \\slash"`,
		"RUBYOPT":           "-w",
	}
	for key, value := range inherited {
		t.Setenv(key, value)
	}
	for _, runtime := range []string{"java", "node", "ruby"} {
		t.Run(runtime, func(t *testing.T) {
			original := os.Environ()
			expected := Prepare(nil, original, dir, Options{Runtimes: []string{runtime}})
			code, script, stderr := captureCLI(t, func() int { return Main([]string{"env", "--format", "sh", runtime}) })
			if code != 0 || stderr != "" || strings.Contains(script, "***") || strings.Contains(script, "VERIFY_OPTION_SECRET") {
				t.Fatal("unsafe generated script")
			}
			// Shell execution is synchronous and exercises quote/metacharacter safety.
			script += "printf '%s\\000%s\\000%s' \"$JAVA_TOOL_OPTIONS\" \"$NODE_OPTIONS\" \"$RUBYOPT\""
			code, output, stderr := startupProbe(t, "/bin/sh", dir, original, "-eu", "-c", script)
			actual := strings.Split(output, "\x00")
			want := environmentMap(expected.Environment)
			if code != 0 || stderr != "" || len(actual) != 3 || actual[0] != want["JAVA_TOOL_OPTIONS"] || actual[1] != want["NODE_OPTIONS"] || actual[2] != want["RUBYOPT"] {
				t.Fatal("sourcing env script changed injection or inherited options")
			}
		})
	}
}

func TestNodeOrdinaryPreloadsAreInstrumented(t *testing.T) {
	if os.Getenv("TRACEAI_E2E") != "1" {
		t.Skip("real Node startup requires TRACEAI_E2E=1")
	}
	node := requireProgram(t, "node")
	for _, mode := range []string{"cli", "environment", "multiple", "failure"} {
		t.Run(mode, func(t *testing.T) {
			dir := t.TempDir()
			put(t, filepath.Join(dir, "node_modules/@traceai/auto/register.js"), "global.AGENT_STARTS = (global.AGENT_STARTS || 0) + 1;")
			put(t, filepath.Join(dir, "request.cjs"), "if (global.AGENT_STARTS !== 1) throw new Error('ordinary preload ran before agent'); global.PRELOAD_RAN = true; module.exports = 37;")
			put(t, filepath.Join(dir, "second.cjs"), "if (require('./request.cjs') !== 37) throw new Error('require return changed');")
			put(t, filepath.Join(dir, "failure.cjs"), "throw new Error('APPLICATION_PRELOAD_FAILURE');")
			env := environmentMap(os.Environ())
			env["TRACEAI_CACHE_DIR"], env["TRACEAI_NODE_AGENT"], env["NODE_OPTIONS"] = filepath.Join(dir, "cache"), "/missing-agent", ""
			args := []string{"-r", "./request.cjs", "-e", "if (!global.PRELOAD_RAN) throw new Error('missing preload'); console.log('APP_RAN')"}
			if mode == "environment" {
				env["NODE_OPTIONS"] = "--require ./request.cjs"
				args = args[2:]
			}
			if mode == "multiple" {
				args = append([]string{"-r", "./second.cjs"}, args...)
			}
			if mode == "failure" {
				args = []string{"-r", "./failure.cjs", "-e", "console.log('APP_RAN')"}
			}
			original := environmentList(env)
			plan := Prepare(nil, original, dir, Options{Runtimes: []string{"node"}})
			code, output, stderr := startupProbe(t, node, dir, plan.Environment, args...)
			if mode == "failure" {
				nativeCode, nativeOut, nativeErr := startupProbe(t, node, dir, original, args...)
				if code != nativeCode || output != nativeOut || !strings.Contains(stderr, "APPLICATION_PRELOAD_FAILURE") || !strings.Contains(nativeErr, "APPLICATION_PRELOAD_FAILURE") || strings.Contains(output, "APP_RAN") {
					t.Fatal("application preload exception changed")
				}
			} else if code != 0 || strings.TrimSpace(output) != "APP_RAN" || stderr != "" {
				t.Fatal("ordinary preload ran without instrumentation", code, output, stderr)
			}
		})
	}
}

func TestNodePreloadRequestsExportBeforeExit(t *testing.T) {
	if os.Getenv("TRACEAI_E2E") != "1" || Flavour != "full" {
		t.Skip("full Node preload wire check requires TRACEAI_E2E=1 and embedagents")
	}
	node := requireProgram(t, "node")
	for _, mode := range []string{"cli", "environment"} {
		t.Run(mode, func(t *testing.T) {
			m := newMock(t)
			dir := t.TempDir()
			put(t, filepath.Join(dir, "request.cjs"), `(async function(){var r=await fetch(process.env.FI_BASE_URL+'/v1/chat/completions',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({model:'launcher-model',messages:[{role:'user',content:'hello'}]})});var result=await r.json();if(result.choices[0].message.content!=='launcher-ok')throw new Error('response changed');console.log('REQUEST_OK');})().catch(function(e){console.error(e);process.exitCode=1;});`)
			env := environmentMap(testEnv(t, m, repoRoot(t)))
			delete(env, "TRACEAI_NODE_AGENT")
			args := []string{"-r", "./request.cjs", "-e", "console.log('MAIN_RAN')"}
			if mode == "environment" {
				env["NODE_OPTIONS"] = "--require ./request.cjs"
				args = args[2:]
			}
			plan := Prepare(nil, environmentList(env), dir, Options{Runtimes: []string{"node"}})
			code, output, stderr := startupProbe(t, node, dir, plan.Environment, args...)
			if code != 0 || !strings.Contains(output, "REQUEST_OK") || !strings.Contains(output, "MAIN_RAN") {
				t.Fatal("preload request changed", code, output, stderr)
			}
			// Process termination is the flush barrier. No polling or sleep for exports.
			m.mu.Lock()
			defer m.mu.Unlock()
			if m.calls != 1 || len(m.spans) != 1 || !strings.Contains(string(m.spans[0]), "traceai.capture.layer") {
				t.Fatal("preload wire request was not exported", m.calls, len(m.spans))
			}
		})
	}
}

func TestGoOutputdirCommandStillInjects(t *testing.T) {
	if os.Getenv("TRACEAI_E2E") != "1" {
		t.Skip("real Go test injection requires TRACEAI_E2E=1")
	}
	dir, agent := t.TempDir(), t.TempDir()
	put(t, filepath.Join(dir, "go.mod"), "module example.com/outputdir-app\n\ngo 1.26.0\n")
	put(t, filepath.Join(dir, "app.go"), "package app\n")
	put(t, filepath.Join(dir, "app_test.go"), "package app\nimport \"testing\"\nfunc TestApp(t *testing.T) {}\n")
	put(t, filepath.Join(agent, "go.mod"), "module "+agentModule+"\n\ngo 1.26.0\n")
	put(t, filepath.Join(agent, "auto.go"), "package auto\nimport \"fmt\"\nfunc init(){fmt.Println(\"AGENT_INIT\")}\n")
	for _, explicit := range []bool{false, true} {
		t.Run(fmt.Sprint(explicit), func(t *testing.T) {
			env := environmentMap(os.Environ())
			env["GOFLAGS"], env["GOWORK"], env["GOPROXY"] = "-p=2", "", "off"
			args := []string{"test", "-count=1", "-v", "-outputdir", filepath.Join(dir, "results")}
			if explicit {
				args = append(args, ".")
			}
			warning := prepareGo(append([]string{"go"}, args...), dir, filepath.Join(t.TempDir(), "cache"), agent, env, func(k, v string) { env[k] = v })
			if warning != "" || env["GOWORK"] == "" {
				t.Fatal("outputdir prevented injection")
			}
			output := runRegressionGo(t, dir, environmentList(env), args...)
			if strings.Count(output, "AGENT_INIT") != 1 || !strings.Contains(output, "PASS") {
				t.Fatal("go test did not initialize the agent")
			}
		})
	}
}

func TestJavaLiteralBackslashPremainRunsOnce(t *testing.T) {
	if os.Getenv("TRACEAI_E2E") != "1" {
		t.Skip("real JVM option parsing requires TRACEAI_E2E=1")
	}
	home := "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home/bin"
	java, javac := requireProgram(t, filepath.Join(home, "java")), requireProgram(t, filepath.Join(home, "javac"))
	dir := t.TempDir()
	put(t, filepath.Join(dir, "LauncherAgent.java"), "public class LauncherAgent { public static void premain(String a, java.lang.instrument.Instrumentation i) { System.out.println(\"AGENT_INIT\"); } }")
	put(t, filepath.Join(dir, "ProbeApp.java"), "public class ProbeApp { public static void main(String[] a) { System.out.println(\"APP_RAN\"); } }")
	env := environmentMap(os.Environ())
	env["JAVA_TOOL_OPTIONS"], env["JDK_JAVA_OPTIONS"], env["_JAVA_OPTIONS"] = "", "", ""
	code, _, _ := startupProbe(t, javac, dir, environmentList(env), "LauncherAgent.java", "ProbeApp.java")
	if code != 0 {
		t.Fatal("could not compile JVM regression probe")
	}
	for _, name := range []string{`agent\path.jar`, `agent with space\path.jar`} {
		t.Run(name, func(t *testing.T) {
			jar := filepath.Join(dir, name)
			file, err := os.Create(jar)
			if err != nil {
				t.Fatal(err)
			}
			archive := zip.NewWriter(file)
			for entry, body := range map[string]string{"META-INF/MANIFEST.MF": "Manifest-Version: 1.0\nPremain-Class: LauncherAgent\n\n", "LauncherAgent.class": ""} {
				if entry == "LauncherAgent.class" {
					data, e := os.ReadFile(filepath.Join(dir, entry))
					if e != nil {
						t.Fatal(e)
					}
					body = string(data)
				}
				writer, e := archive.Create(entry)
				if e != nil {
					t.Fatal(e)
				}
				if _, e = writer.Write([]byte(body)); e != nil {
					t.Fatal(e)
				}
			}
			if err = archive.Close(); err != nil {
				t.Fatal(err)
			}
			if err = file.Close(); err != nil {
				t.Fatal(err)
			}
			env["TRACEAI_JAVA_AGENT"], env["TRACEAI_CACHE_DIR"] = jar, filepath.Join(t.TempDir(), "cache")
			first := Prepare(nil, environmentList(env), dir, Options{Runtimes: []string{"java"}})
			second := Prepare(nil, first.Environment, dir, Options{Runtimes: []string{"java"}})
			code, output, _ := startupProbe(t, java, dir, second.Environment, "-cp", dir, "ProbeApp")
			if code != 0 || output != "AGENT_INIT\nAPP_RAN\n" {
				t.Fatal("nested literal-backslash agent did not run exactly once", code)
			}
			// Exercise the original env-script reproduction with a valid agent:
			// sourcing it must retain -Xmx and successfully start the JVM.
			env["JAVA_TOOL_OPTIONS"] = "-Xmx128m"
			for _, key := range []string{"JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "TRACEAI_JAVA_AGENT", "TRACEAI_CACHE_DIR"} {
				t.Setenv(key, env[key])
			}
			envCode, script, _ := captureCLI(t, func() int { return envCLI([]string{"java"}, dir) })
			if envCode != 0 || strings.Contains(script, "***") || strings.Contains(script, "-Xmx128m") {
				t.Fatal("JVM env script replaced or disclosed inherited options")
			}
			script += "exec " + shellQuote(java) + " -cp " + shellQuote(dir) + " ProbeApp"
			code, output, _ = startupProbe(t, "/bin/sh", dir, environmentList(env), "-eu", "-c", script)
			if code != 0 || output != "AGENT_INIT\nAPP_RAN\n" {
				t.Fatal("sourcing generated JVM environment broke startup", code)
			}
		})
	}
}
