//go:build !windows

package launcher

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func runRegressionGo(t *testing.T, dir string, env []string, args ...string) string {
	t.Helper()
	hostGuard(t)
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, requireProgram(t, "go"), args...)
	cmd.Dir, cmd.Env = dir, env
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("go %v: %v\n%s", args, err, output)
	}
	return string(output)
}

func TestGoRegressionProcesses(t *testing.T) {
	if os.Getenv("TRACEAI_E2E") != "1" {
		t.Skip("serialized real-Go regression checks require TRACEAI_E2E=1")
	}
	for _, mode := range []string{"vendored_patch", "literal_backslash_goflags", "trailing_test_mod", "trailing_test_modfile", "test_only_agent_import", "go_run_application_flag", "merged_goflags_overlay", "persisted_goflags", "empty_goflags", "go_run_flag_terminator"} {
		t.Run(mode, func(t *testing.T) {
			dir := t.TempDir()
			agent := filepath.Join(t.TempDir(), "agent")
			put(t, filepath.Join(agent, "go.mod"), "module "+agentModule+"\n\ngo 1.26.0\n")
			put(t, filepath.Join(agent, "agent.go"), "package auto\nimport \"fmt\"\nfunc init(){fmt.Println(\"AGENT_INIT\")}\n")
			mod := "module example.com/regression\n\ngo 1.26.0\n"
			main := "package main\nimport \"fmt\"\nvar root=\"APP_ONLY\"\nfunc main(){fmt.Println(root)}\n"
			env := environmentMap(os.Environ())
			env["GOFLAGS"], env["GOWORK"], env["GOPROXY"] = "-p=2", "", "off"
			env["GOTOOLCHAIN"], env["GOMAXPROCS"] = "auto", "4"
			args := []string{"run", "."}
			want, inject := "APP_ONLY", true
			switch mode {
			case "vendored_patch":
				upstream := filepath.Join(t.TempDir(), "dependency")
				put(t, filepath.Join(upstream, "go.mod"), "module example.com/dependency\n\ngo 1.26.0\n")
				put(t, filepath.Join(upstream, "dependency.go"), "package dependency\nconst Value=\"UPSTREAM\"\n")
				mod += "\nrequire example.com/dependency v0.0.0\nreplace example.com/dependency => " + upstream + "\n"
				put(t, filepath.Join(dir, "vendor/example.com/dependency/dependency.go"), "package dependency\nconst Value=\"VENDORED_PATCH\"\n")
				put(t, filepath.Join(dir, "vendor/modules.txt"), "# example.com/dependency v0.0.0 => "+upstream+"\n## explicit; go 1.26.0\nexample.com/dependency\n# example.com/dependency => "+upstream+"\n")
				main = "package main\nimport(\"fmt\";\"example.com/dependency\")\nfunc main(){fmt.Println(dependency.Value)}\n"
				want, inject = "VENDORED_PATCH", false
			case "go_run_flag_terminator":
				put(t, filepath.Join(dir, "cmd/app/main.go"), main)
				main = "package library\n"
				args = []string{"run", "--", "./cmd/app", "--application-flag"}
			case "persisted_goflags", "empty_goflags":
				goenv := filepath.Join(t.TempDir(), "goenv")
				put(t, goenv, "GOFLAGS=-p=2 -tags=production -ldflags=-X=main.root=LINKER_KEPT\n")
				env["GOENV"] = goenv
				delete(env, "GOFLAGS")
				if mode == "empty_goflags" {
					env["GOFLAGS"] = ""
				}
				main = "//go:build production\n\n" + main
				put(t, filepath.Join(dir, "default.go"), "//go:build !production\n\npackage main\nimport \"fmt\"\nfunc main(){fmt.Println(\"DEFAULT\")}\n")
				want = "LINKER_KEPT"
			case "literal_backslash_goflags":
				env["GOFLAGS"] = `-p=2 -ldflags=-X=main.root=C:\data`
				want = `C:\data`
			case "trailing_test_mod":
				args, want, inject = []string{"test", ".", "-mod=mod"}, "[no test files]", false
			case "trailing_test_modfile":
				put(t, filepath.Join(dir, "alternate.mod"), mod)
				args, want, inject = []string{"test", ".", "-modfile=alternate.mod"}, "[no test files]", false
			case "test_only_agent_import":
				mod += "\nrequire " + agentModule + " v0.0.0\nreplace " + agentModule + " => " + agent + "\n"
				put(t, filepath.Join(dir, "main_test.go"), "package main\nimport _ \""+agentModule+"\"\n")
			case "go_run_application_flag":
				args = []string{"run", ".", "-mod=mod"}
			case "merged_goflags_overlay":
				alternate := filepath.Join(t.TempDir(), "alternate.go")
				put(t, alternate, strings.ReplaceAll(main, "APP_ONLY", "OVERLAY_KEPT"))
				mapping := "{\"Replace\":{" + strconv.Quote(filepath.Join(dir, "main.go")) + ":" + strconv.Quote(alternate) + "}}"
				overlay := filepath.Join(dir, "original-overlay.json")
				put(t, overlay, mapping)
				env["GOFLAGS"] += " -overlay=" + overlay
				want = "OVERLAY_KEPT"
			}
			put(t, filepath.Join(dir, "go.mod"), mod)
			put(t, filepath.Join(dir, "main.go"), main)
			original := environmentList(env)
			before := runRegressionGo(t, dir, original, args...)
			if !strings.Contains(before, want) {
				t.Fatal("direct command did not establish baseline", before)
			}
			warning := prepareGo(append([]string{"go"}, args...), dir, filepath.Join(t.TempDir(), "cache"), agent, env, func(k, v string) { env[k] = v })
			if inject && (warning != "" || env["GOWORK"] == "") {
				t.Fatal("injection failed", warning, env["GOFLAGS"])
			}
			if !inject && (warning == "" || env["GOWORK"] != "" || env["GOFLAGS"] != environmentMap(original)["GOFLAGS"]) {
				t.Fatal("incompatible command was changed", warning)
			}
			wantFlags := environmentMap(original)["GOFLAGS"]
			if mode == "persisted_goflags" || mode == "empty_goflags" {
				wantFlags = "-p=2 -tags=production -ldflags=-X=main.root=LINKER_KEPT"
				if data, _ := os.ReadFile(env["GOENV"]); string(data) != "GOFLAGS="+wantFlags+"\n" {
					t.Fatal("persisted Go configuration changed")
				}
			}
			if inject && !strings.HasPrefix(env["GOFLAGS"], wantFlags+" ") {
				t.Fatal("rewrote inherited GOFLAGS")
			}
			after := runRegressionGo(t, dir, environmentList(env), args...)
			if !strings.Contains(after, want) || strings.Contains(after, "AGENT_INIT") != inject {
				t.Fatalf("changed behavior or missing instrumentation: %q", after)
			}
			if data, _ := os.ReadFile(filepath.Join(dir, "go.mod")); string(data) != mod {
				t.Fatal("go.mod changed")
			}
		})
	}
}

func TestNodeLiteralPathProcess(t *testing.T) {
	if os.Getenv("TRACEAI_E2E") != "1" {
		t.Skip("serialized real-Node path checks require TRACEAI_E2E=1")
	}
	node := requireProgram(t, "node")
	for _, name := range []string{"cache\ttab", "cache'quote", "cache\"quote", `cache\slash`, "cache\u0085unicode"} {
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			env := append(os.Environ(), "TRACEAI_CACHE_DIR="+filepath.Join(dir, name), "TRACEAI_NODE_AGENT=/not-installed", "NODE_OPTIONS=")
			first := Prepare(nil, env, dir, Options{Runtimes: []string{"node"}})
			second := Prepare(nil, first.Environment, dir, Options{Runtimes: []string{"node"}})
			hostGuard(t)
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			cmd := exec.CommandContext(ctx, node, "-e", "console.log('APP')")
			cmd.Dir, cmd.Env = dir, second.Environment
			output, err := cmd.CombinedOutput()
			if err != nil || strings.TrimSpace(string(output)) != "APP" {
				t.Fatal(err, string(output))
			}
		})
	}
}

func TestRubyApostropheNestedProcess(t *testing.T) {
	if os.Getenv("TRACEAI_E2E") != "1" {
		t.Skip("serialized real-Ruby path check requires TRACEAI_E2E=1")
	}
	ruby := requireProgram(t, "/opt/homebrew/opt/ruby@3.2/bin/ruby")
	dir := t.TempDir()
	env := append(os.Environ(), "TRACEAI_CACHE_DIR="+filepath.Join(dir, "ruby'cache"), "TRACEAI_RUBY_AGENT=/not-installed", "RUBYOPT=")
	first := Prepare(nil, env, dir, Options{Runtimes: []string{"ruby"}})
	second := Prepare(nil, first.Environment, dir, Options{Runtimes: []string{"ruby"}})
	hostGuard(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, ruby, "-e", `puts ENV.fetch('RUBYOPT').scan(/-rtraceai_launcher_/).length`)
	cmd.Dir, cmd.Env = dir, second.Environment
	output, err := cmd.CombinedOutput()
	if err != nil || strings.TrimSpace(string(output)) != "1" {
		t.Fatal(err, string(output))
	}
}
