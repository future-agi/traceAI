//go:build !windows

package launcher

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func executable(t *testing.T, path, source string) {
	t.Helper()
	put(t, path, "#!/bin/sh\n"+source+"\n")
	if err := os.Chmod(path, 0700); err != nil {
		t.Fatal(err)
	}
}

func TestInstallPackageManagers(t *testing.T) {
	for _, tc := range []struct{ runtime, manager, lock, want string }{
		{"python", "python3", "", "-m pip install fi-instrumentation-otel"},
		{"python", "uv", "uv.lock", "pip install --python"},
		{"node", "npm", "package-lock.json", "install @traceai/auto"},
		{"node", "pnpm", "pnpm-lock.yaml", "add @traceai/auto"},
		{"node", "yarn", "yarn.lock", "add @traceai/auto"},
		{"node", "bun", "bun.lock", "add @traceai/auto"},
		{"ruby", "gem", "", "install traceai-auto"},
	} {
		t.Run(tc.runtime+"_"+tc.manager, func(t *testing.T) {
			dir := isolatedCLI(t)
			bin := filepath.Join(dir, "tools")
			log := filepath.Join(dir, "install.log")
			executable(t, filepath.Join(bin, tc.manager), "printf '%s\\n' \"$*\" > "+shellQuote(log))
			if tc.manager == "uv" {
				executable(t, filepath.Join(bin, "python3"), "exit 0")
			}
			t.Setenv("PATH", bin)
			t.Setenv("TRACEAI_PYTHON", filepath.Join(bin, "python3"))
			t.Setenv("VIRTUAL_ENV", "")
			if tc.lock != "" {
				put(t, filepath.Join(dir, tc.lock), "lock")
			}
			code, out, stderr := captureCLI(t, func() int { return Main([]string{"install", tc.runtime}) })
			data, _ := os.ReadFile(log)
			if code != 0 || !strings.Contains(string(data), tc.want) {
				t.Fatalf("%d %s %s log=%s", code, out, stderr, data)
			}
		})
	}
}

func TestDoctorCommandMasksAvailability(t *testing.T) {
	dir := isolatedCLI(t)
	bin := filepath.Join(dir, "tools")
	for _, name := range []string{"python3", "node", "ruby"} {
		executable(t, filepath.Join(bin, name), "printf '%s' '/project/command-api-key/agent'")
	}
	t.Setenv("PATH", bin)
	t.Setenv("TRACEAI_PYTHON", filepath.Join(bin, "python3"))
	code, out, stderr := captureCLI(t, func() int { return Main([]string{"doctor", "--offline", "--json"}) })
	if code != 0 || !strings.Contains(out, `"runtimes"`) || strings.Contains(out+stderr, "command-api-key") || strings.Contains(out+stderr, "command-secret-key") {
		t.Fatalf("%d %s %s", code, out, stderr)
	}
	var report map[string]any
	if err := json.Unmarshal([]byte(out), &report); err != nil {
		t.Fatal(err)
	}
	if len(report["runtimes"].([]any)) != 6 {
		t.Fatal(report)
	}
}

func TestSetupGoCommand(t *testing.T) {
	for _, success := range []bool{true, false} {
		t.Run(map[bool]string{true: "success", false: "rollback"}[success], func(t *testing.T) {
			dir := isolatedCLI(t)
			put(t, filepath.Join(dir, "go.mod"), "module example.com/setup\n\ngo 1.26.0\n")
			put(t, filepath.Join(dir, "main.go"), "package main\nfunc main(){}\n")
			bin := filepath.Join(dir, "tools")
			log := filepath.Join(dir, "get.log")
			pkg := goPackage{Dir: dir, Name: "main"}
			pkg.Module = &struct{ Path, Dir, GoVersion string }{"example.com/setup", dir, "1.26.0"}
			data, _ := json.Marshal(pkg)
			get := "printf '%s' \"$*\" > " + shellQuote(log)
			if !success {
				get += "; exit 1"
			}
			executable(t, filepath.Join(bin, "go"), "if test \"$1\" = list; then printf '%s' "+shellQuote(string(data))+"; else "+get+"; fi")
			t.Setenv("PATH", bin)
			code, out, stderr := captureCLI(t, func() int { return Main([]string{"setup", "go"}) })
			if (code == 0) != success {
				t.Fatalf("%d %s %s", code, out, stderr)
			}
			path := filepath.Join(dir, "traceai_auto.go")
			content, err := os.ReadFile(path)
			if success && (err != nil || !strings.Contains(string(content), agentModule)) {
				t.Fatal("import file missing", err)
			}
			if !success && !os.IsNotExist(err) {
				t.Fatal("failed setup did not roll back")
			}
			invoked, _ := os.ReadFile(log)
			if string(invoked) != "get "+agentModule {
				t.Fatal("wrong go get", string(invoked))
			}
			if success {
				code, _, _ = captureCLI(t, func() int { return Main([]string{"setup", "go"}) })
				after, _ := os.ReadFile(path)
				if code != 1 || string(after) != string(content) {
					t.Fatal("setup overwrote existing file")
				}
			}
		})
	}
}

func TestInstallLocalCLI(t *testing.T) {
	isolatedCLI(t)
	for _, runtime := range []string{"java", "dotnet", "node", "go", "ruby"} {
		t.Run(runtime, func(t *testing.T) {
			source := t.TempDir()
			put(t, filepath.Join(source, agentEntry(runtime)), "agent")
			code, out, stderr := captureCLI(t, func() int { return Main([]string{"install", runtime, "--from", source}) })
			if code != 0 {
				t.Fatalf("%d %s %s", code, out, stderr)
			}
		})
	}
}

func rubyProbe(ruby, dir string, env []string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, ruby, "-e", "puts ENV.fetch('RUBY_AGENT_CHOSEN', 'none')")
	cmd.Dir, cmd.Env = dir, env
	return cmd.CombinedOutput()
}

func TestRubyShimFailOpenAndProjectPrecedence(t *testing.T) {
	// This test exercises shim semantics without requiring the unfinished gem.
	// The real Ruby transport/export test is separately opt-in.
	if os.Getenv("TRACEAI_E2E") != "1" {
		t.Skip("Ruby child-process checks are part of TRACEAI_E2E")
	}
	ruby := requireProgram(t, "/opt/homebrew/opt/ruby@3.2/bin/ruby")
	for _, mode := range []string{"project", "broken", "missing"} {
		t.Run(mode, func(t *testing.T) {
			dir := t.TempDir()
			fallback := filepath.Join(dir, "fallback")
			put(t, filepath.Join(fallback, "traceai/auto.rb"), "ENV['RUBY_AGENT_CHOSEN']='fallback'")
			env := append(baseEnv(t), "TRACEAI_RUBY_AGENT="+fallback, "TRACEAI_CACHE_DIR="+filepath.Join(dir, "cache with spaces"))
			want := "fallback"
			if mode == "project" {
				project := filepath.Join(dir, "project")
				put(t, filepath.Join(project, "traceai/auto.rb"), "ENV['RUBY_AGENT_CHOSEN']='project'")
				env = append(env, "RUBYLIB="+project)
				want = "project"
			}
			if mode == "broken" {
				put(t, filepath.Join(fallback, "traceai/auto.rb"), "raise Exception, 'broken agent'")
				want = "none"
			}
			if mode == "missing" {
				env = append(env, "TRACEAI_RUBY_AGENT=/missing-ruby-agent")
				want = "none"
			}
			plan := Prepare(nil, env, dir, Options{Runtimes: []string{"ruby"}})
			hostGuard(t)
			output, err := rubyProbe(ruby, dir, plan.Environment)
			if err != nil || strings.TrimSpace(string(output)) != want {
				t.Fatalf("%s: %v %s", mode, err, output)
			}
		})
	}
}
