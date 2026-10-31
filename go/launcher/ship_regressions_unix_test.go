//go:build !windows

package launcher

import (
	"archive/zip"
	"bytes"
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

func TestGoInjectionRequiresOptIn(t *testing.T) {
	for _, mode := range []string{"default", "environment_zero", "environment_true", "environment_one", "flag"} {
		t.Run(mode, func(t *testing.T) {
			dir, agent, env := fakeGoProject(t)
			env["TRACEAI_CACHE_DIR"], env["TRACEAI_GO_AGENT"] = filepath.Join(t.TempDir(), "cache"), agent
			opts := Options{Runtimes: []string{"go"}, GoInject: mode == "flag"}
			switch mode {
			case "environment_zero":
				env["TRACEAI_GO_INJECT"] = "0"
			case "environment_true":
				env["TRACEAI_GO_INJECT"] = "true"
			case "environment_one":
				env["TRACEAI_GO_INJECT"] = "1"
			}
			plan := Prepare([]string{"go", "run", "."}, environmentList(env), dir, opts)
			injected := environmentMap(plan.Environment)["GOWORK"] != ""
			if injected != (mode == "flag" || mode == "environment_one") {
				t.Fatal("Go injection opt-in was not honored")
			}
			if !injected {
				if len(plan.Warnings) != 1 || !strings.Contains(plan.Warnings[0], "traceai setup go") {
					t.Fatal("default Go setup hint missing")
				}
				nested := Prepare([]string{"go", "run", "."}, plan.Environment, dir, opts)
				if len(nested.Warnings) != 0 || !reflect.DeepEqual(plan.Environment, nested.Environment) {
					t.Fatal("nested launcher repeats the Go hint or changes flags")
				}
			}
		})
	}
}

func TestDefaultGoNeverInvokesToolchain(t *testing.T) {
	for _, sub := range []string{"run", "build", "test", "install"} {
		t.Run(sub, func(t *testing.T) {
			dir := t.TempDir()
			marker := filepath.Join(dir, "toolchain-was-called")
			executable(t, filepath.Join(dir, "go"), "printf CALLED > "+shellQuote(marker)+"; exit 9")
			env := map[string]string{"PATH": dir, "TRACEAI_CACHE_DIR": filepath.Join(dir, "cache"), "GOFLAGS": "opaque application flags", "GOWORK": "application workspace"}
			plan := Prepare([]string{"go", sub, "."}, environmentList(env), dir, Options{Runtimes: []string{"go"}})
			actual := environmentMap(plan.Environment)
			if actual["GOFLAGS"] != env["GOFLAGS"] || actual["GOWORK"] != env["GOWORK"] || plan.Changes["GOFLAGS"] != "" || plan.Changes["GOWORK"] != "" {
				t.Fatal("default command changed Go flags")
			}
			if _, err := os.Stat(marker); !os.IsNotExist(err) {
				t.Fatal("default handling invoked the toolchain")
			}
		})
	}
}

func TestGoInjectCLIFlag(t *testing.T) {
	dir := isolatedCLI(t)
	project, agent, env := fakeGoProject(t)
	t.Chdir(project)
	for key, value := range env {
		t.Setenv(key, value)
	}
	t.Setenv("TRACEAI_GO_AGENT", agent)
	t.Setenv("TRACEAI_GO_INJECT", "0")
	t.Setenv("GOWORK", "")
	code, output, _ := captureCLI(t, func() int { return Main([]string{"run", "--dry-run", "--go-inject", "--", "go", "build", "."}) })
	if code != 0 || !strings.Contains(output, `"GOWORK"`) || !strings.Contains(output, dir) {
		t.Fatal("CLI flag did not activate the experimental overlay")
	}
}

func TestAgentOverrideSpecialFilesFailOpen(t *testing.T) {
	if os.Getenv("TRACEAI_TEST_OVERRIDE_FIFO") == "1" {
		os.Exit(Main([]string{"run", "--", "/usr/bin/printf", "APP_RAN"}))
	}
	for _, language := range []string{"java", "dotnet", "node", "go", "ruby"} {
		for _, shape := range []string{"file", "directory", "symlink"} {
			t.Run(language+"/"+shape, func(t *testing.T) {
				dir := t.TempDir()
				pipe := filepath.Join(dir, filepath.FromSlash(agentEntry(language)))
				if err := os.MkdirAll(filepath.Dir(pipe), 0700); err != nil {
					t.Fatal(err)
				}
				if err := syscall.Mkfifo(pipe, 0600); err != nil {
					t.Fatal(err)
				}
				override := pipe
				if shape == "directory" {
					override = dir
				} else if shape == "symlink" {
					override = filepath.Join(dir, "agent-link")
					if err := os.Symlink(pipe, override); err != nil {
						t.Fatal(err)
					}
				}
				ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer cancel()
				cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestAgentOverrideSpecialFilesFailOpen$")
				cmd.Env = []string{"TRACEAI_TEST_OVERRIDE_FIFO=1", "TRACEAI_CACHE_DIR=" + filepath.Join(dir, "cache"), "TRACEAI_" + strings.ToUpper(language) + "_AGENT=" + override}
				output, err := cmd.CombinedOutput()
				if err != nil || string(output) != "APP_RAN" {
					t.Fatal("special-file override blocked or changed application startup")
				}
			})
		}
	}
}

func TestExecutableEnvOutputAlwaysMergesLateVariables(t *testing.T) {
	dir := isolatedCLI(t)
	// Whitespace in the cache exercises Ruby's RUBYLIB fallback too.
	t.Setenv("TRACEAI_CACHE_DIR", filepath.Join(dir, "cache with space"))
	put(t, filepath.Join(dir, "agent.jar"), "jar")
	put(t, filepath.Join(dir, "hook.dll"), "dll")
	t.Setenv("TRACEAI_JAVA_AGENT", filepath.Join(dir, "agent.jar"))
	t.Setenv("TRACEAI_DOTNET_AGENT", filepath.Join(dir, "hook.dll"))
	keys := []string{"PYTHONPATH", "NODE_OPTIONS", "JAVA_TOOL_OPTIONS", "DOTNET_STARTUP_HOOKS", "RUBYOPT", "RUBYLIB"}
	for _, state := range []string{"absent", "empty"} {
		t.Run(state, func(t *testing.T) {
			for _, key := range keys {
				t.Setenv(key, "")
				if state == "absent" {
					if err := os.Unsetenv(key); err != nil {
						t.Fatal(err)
					}
				}
			}
			for _, format := range []string{"sh", "fish", "powershell", "dockerfile"} {
				t.Run(format, func(t *testing.T) {
					code, script, stderr := captureCLI(t, func() int { return Main([]string{"env", "--format", format}) })
					if code != 0 || stderr != "" {
						t.Fatal("env generation failed")
					}
					for _, key := range keys {
						reference := "$" + key
						if format == "sh" || format == "dockerfile" {
							reference = "${" + key
						} else if format == "powershell" {
							reference = "$env:" + key
						}
						if !strings.Contains(script, reference) {
							t.Fatal("late-populated variable would be clobbered", key)
						}
					}
					if format != "sh" {
						return // These renderers have syntax assertions, not installed-shell execution.
					}
					env := environmentMap(os.Environ())
					for _, key := range keys {
						env[key] = "later 'quoted' $literal \\value"
					}
					original := environmentList(env)
					want := environmentMap(Prepare(nil, original, dir, Options{}).Environment)
					for _, key := range keys {
						script += "printf '%s\\000' \"$" + key + "\"\n"
					}
					code, output, stderr := startupProbe(t, "/bin/sh", dir, original, "-eu", "-c", script)
					actual := strings.Split(output, "\x00")
					if code != 0 || stderr != "" || len(actual) != len(keys)+1 {
						t.Fatal("late-variable script execution failed")
					}
					for i, key := range keys {
						if actual[i] != want[key] {
							t.Fatal("late variable was changed", key)
						}
					}
				})
			}
		})
	}
}

func TestPythonMissingPackageDiagnosticHasNoInterpreterPath(t *testing.T) {
	if os.Getenv("TRACEAI_E2E") != "1" {
		t.Skip("real Python startup requires TRACEAI_E2E=1")
	}
	python := requireProgram(t, "python3")
	dir := t.TempDir()
	secret := "VERIFY_DUMMY_EXEC_PATH_SECRET_82041"
	link := filepath.Join(dir, secret)
	if err := os.Symlink(python, link); err != nil {
		t.Fatal(err)
	}
	env := environmentMap(pythonStartupEnvironment(t, dir))
	put(t, filepath.Join(dir, "project/fi_instrumentation/auto/_bootstrap.py"), "raise ImportError('test missing package')\n")
	env["FI_API_KEY"], env["TRACEAI_DEBUG"] = secret, "1"
	plan := Prepare(nil, environmentList(env), dir, Options{Runtimes: []string{"python"}})
	code, output, stderr := startupProbe(t, link, dir, plan.Environment, "-c", "print('APP_RAN')")
	if code != 0 || output != "APP_RAN\n" || strings.Contains(output+stderr, secret) || strings.Contains(stderr, dir) || strings.Count(stderr, "lacks fi-instrumentation-otel") != 1 {
		t.Fatal("Python shim diagnostic leaked a credential or changed startup")
	}
}

func TestGoStagingExcludesGeneratedAndNestedModules(t *testing.T) {
	python := requireProgram(t, "python3")
	root, dest := t.TempDir(), t.TempDir()
	var jar bytes.Buffer
	archive := zip.NewWriter(&jar)
	if _, err := archive.Create("META-INF/MANIFEST.MF"); err != nil {
		t.Fatal(err)
	}
	if err := archive.Close(); err != nil {
		t.Fatal(err)
	}
	put(t, filepath.Join(root, "java/traceai-javaagent/target/traceai-javaagent.jar"), jar.String())
	put(t, filepath.Join(root, "csharp/src/TraceAI.AutoInstrumentation/bin/Release/net8.0/TraceAI.AutoInstrumentation.dll"), "hook")
	put(t, filepath.Join(root, "csharp/src/TraceAI.AutoInstrumentation/bin/Release/net8.0/TraceAI.AutoInstrumentation.deps.json"), `{"runtimeTarget":{"name":".NETCoreApp,Version=v8.0"},"libraries":{"TraceAI.AutoInstrumentation/0.1.0":{}}}`)
	put(t, filepath.Join(root, "typescript/packages/traceai_auto/dist/agent/preload.cjs"), "agent")
	put(t, filepath.Join(root, "typescript/packages/traceai_auto/package.json"), `{"version":"0.1.0"}`)
	for _, name := range []string{"go.mod", "go.sum", "auto.go", "internal/wire/core.go", "internal/wire/wire-protocols.json"} {
		put(t, filepath.Join(root, "go/auto", name), "agent source")
	}
	for _, name := range []string{".test-results/mod/dependency/go.mod", ".test-results/mod/dependency/library.go", ".scratch/generated.go", "vendor/dep/dep.go", "node_modules/generated.go", "cmd/probe/main.go", "testdata/probe.go", "auto_test.go", "nested/go.mod", "nested/module.go"} {
		put(t, filepath.Join(root, "go/auto", name), "not payload")
	}
	script := filepath.Join(repoRoot(t), "go/launcher/scripts/stage.py")
	code, _, stderr := startupProbe(t, python, t.TempDir(), os.Environ(), "-I", script, root, dest)
	if code != 0 {
		t.Fatal("staging fixture failed", stderr)
	}
	files, err := filesFromFS(os.DirFS(dest), "go")
	if err != nil || len(files) != 5 {
		t.Fatal("generated files or nested modules entered the Go payload", len(files), err)
	}
	for _, name := range []string{"go.mod", "go.sum", "auto.go", "internal/wire/core.go", "internal/wire/wire-protocols.json"} {
		if string(files[name+".source"]) != "agent source" {
			t.Fatal("production source was lost during staging", name)
		}
	}
}

func TestGoMixedTargetsAndProfilingOptIn(t *testing.T) {
	if os.Getenv("TRACEAI_E2E") != "1" {
		t.Skip("real Go overlay regression requires TRACEAI_E2E=1")
	}
	dir, agent := t.TempDir(), t.TempDir()
	put(t, filepath.Join(agent, "go.mod"), "module "+agentModule+"\n\ngo 1.26.0\n")
	put(t, filepath.Join(agent, "auto.go"), "package auto\nimport \"fmt\"\nfunc init(){fmt.Println(\"AGENT_INIT\")}\n")
	put(t, filepath.Join(dir, "go.mod"), fmt.Sprintf("module example.com/mixed\n\ngo 1.26.0\nrequire %s v0.0.0\nreplace %s => %s\n", agentModule, agentModule, agent))
	put(t, filepath.Join(dir, "cmd/a/main.go"), "package main\nimport _ \""+agentModule+"\"\nfunc main(){}\n")
	put(t, filepath.Join(dir, "cmd/b/main.go"), "package main\nimport \"fmt\"\nfunc main(){fmt.Println(\"B\")}\n")
	put(t, filepath.Join(dir, "cmd/b/main_test.go"), "package main\nimport \"testing\"\nfunc TestApp(t *testing.T){}\n")
	env := environmentMap(os.Environ())
	env["GOFLAGS"], env["GOWORK"], env["GOPROXY"] = "-p=2", "", "off"
	for _, sub := range []string{"build", "test"} {
		t.Run(sub, func(t *testing.T) {
			args := []string{"build", "-o", filepath.Join(dir, "out") + string(os.PathSeparator), "./cmd/..."}
			if sub == "test" {
				args = []string{"test", "-v", "-count=1", "-cpuprofile", filepath.Join(dir, "app.cpu"), "./cmd/b"}
			}
			copy := environmentMap(environmentList(env))
			warning := prepareGo(append([]string{"go"}, args...), dir, filepath.Join(t.TempDir(), "cache"), agent, copy, func(k, v string) { copy[k] = v })
			if warning != "" || copy["GOWORK"] == "" {
				t.Fatal("mixed targets or profiling flags suppressed opt-in injection", warning)
			}
			output := runRegressionGo(t, dir, environmentList(copy), args...)
			if sub == "build" {
				for _, target := range []string{"a", "b"} {
					code, out, _ := startupProbe(t, filepath.Join(dir, "out", target), dir, environmentList(env))
					if code != 0 || strings.Count(out, "AGENT_INIT") != 1 {
						t.Fatal("selected executable not instrumented exactly once", target)
					}
				}
			} else if strings.Count(output, "AGENT_INIT") != 1 || !strings.Contains(output, "PASS") {
				t.Fatal("profiled test did not initialize agent")
			}
		})
	}
}
