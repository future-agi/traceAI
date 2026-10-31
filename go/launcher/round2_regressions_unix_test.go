//go:build !windows

package launcher

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

func fakeGoProject(t *testing.T) (string, string, map[string]string) {
	t.Helper()
	dir, agent, bin := t.TempDir(), t.TempDir(), t.TempDir()
	put(t, filepath.Join(dir, "go.mod"), "module example.com/app\n\ngo 1.26.0\n")
	put(t, filepath.Join(agent, "go.mod"), "module "+agentModule+"\n\ngo 1.26.0\n")
	pkg := goPackage{Dir: dir, Name: "main"}
	pkg.Module = &struct{ Path, Dir, GoVersion string }{"example.com/app", dir, "1.26.0"}
	data, err := json.Marshal(pkg)
	if err != nil {
		t.Fatal(err)
	}
	executable(t, filepath.Join(bin, "go"), "case \"$1\" in\nlist) if test \"$2\" = -m; then printf '%s' '{\"Path\":\"example.com/app\"}'; else printf '%s' "+shellQuote(string(data))+"; fi;;\nenv) printf '%s' '{\"GOFLAGS\":\"-p=2 -tags=production\"}';;\n*) exit 9;;\nesac")
	return dir, agent, map[string]string{"PATH": bin, "GOFLAGS": "-p=2"}
}

func TestGoCacheIntermediateDirectoriesFailOpen(t *testing.T) {
	for _, relative := range []string{"go", "go/sources", "go/workspaces", "generated"} {
		for _, attack := range []string{"symlink", "shared"} {
			t.Run(relative+"/"+attack, func(t *testing.T) {
				dir, agent, env := fakeGoProject(t)
				cache := filepath.Join(t.TempDir(), "cache")
				set := func(k, v string) { env[k] = v }
				// Establish the content-addressed workspace path before replacing exactly
				// one directory. No timing or race with a compiler is needed.
				if warning := prepareGo([]string{"go", "run", "."}, dir, cache, agent, env, set); warning != "" {
					t.Fatal(warning)
				}
				target := filepath.Join(cache, filepath.FromSlash(relative))
				if relative == "generated" {
					target = filepath.Dir(env["GOWORK"])
				}
				if err := os.RemoveAll(target); err != nil {
					t.Fatal(err)
				}
				external := filepath.Join(t.TempDir(), "public")
				if err := os.Mkdir(external, 0700); err != nil {
					t.Fatal(err)
				}
				if attack == "symlink" {
					if err := os.Symlink(external, target); err != nil {
						t.Fatal(err)
					}
				} else {
					if err := os.Mkdir(target, 0700); err != nil {
						t.Fatal(err)
					}
					external = target
				}
				if err := os.Chmod(external, 0777); err != nil {
					t.Fatal(err)
				}
				put(t, filepath.Join(external, "sentinel"), "unchanged")
				env["GOFLAGS"] = "-p=2"
				delete(env, "GOWORK")
				before := environmentList(env)
				warning := prepareGo([]string{"go", "run", "."}, dir, cache, agent, env, set)
				if warning == "" || !reflect.DeepEqual(before, environmentList(env)) {
					t.Fatal("unsafe cache activated", warning, env)
				}
				entries, err := os.ReadDir(external)
				if err != nil || len(entries) != 1 || entries[0].Name() != "sentinel" {
					t.Fatal("wrote through unsafe directory", entries, err)
				}
				if info, err := os.Stat(external); err != nil || info.Mode().Perm() != 0777 {
					t.Fatal("changed caller-owned directory", err)
				}
			})
		}
	}
}

func TestGoEffectiveFlagsConfiguration(t *testing.T) {
	for _, mode := range []string{"absent", "empty", "explicit", "invalid", "unavailable", "incompatible"} {
		t.Run(mode, func(t *testing.T) {
			dir, agent, env := fakeGoProject(t)
			delete(env, "GOFLAGS")
			switch mode {
			case "empty":
				env["GOFLAGS"] = ""
			case "explicit":
				env["GOFLAGS"] = "-p=1 -tags=override"
			case "invalid":
				executable(t, filepath.Join(env["PATH"], "go"), "printf '%s' '{invalid-json'")
			case "unavailable":
				executable(t, filepath.Join(env["PATH"], "go"), "exit 1")
			case "incompatible":
				executable(t, filepath.Join(env["PATH"], "go"), "printf '%s' '{\"GOFLAGS\":\"-mod=mod\"}'")
			}
			original := environmentList(env)
			warning := prepareGo([]string{"go", "run", "."}, dir, filepath.Join(t.TempDir(), "cache"), agent, env, func(k, v string) { env[k] = v })
			if mode == "invalid" || mode == "unavailable" || mode == "incompatible" {
				if warning == "" || !reflect.DeepEqual(original, environmentList(env)) {
					t.Fatal("configuration failure changed command", warning, env)
				}
				return
			}
			want := "-p=2 -tags=production"
			if mode == "explicit" {
				want = "-p=1 -tags=override"
			}
			if warning != "" || env["GOWORK"] == "" || !strings.HasPrefix(env["GOFLAGS"], want+" -overlay=") {
				t.Fatal(warning, env)
			}
		})
	}
}

// Wait for actual process termination: startup behavior must not depend on a
// timer, file polling, or an agent flush interval.
func startupProbe(t *testing.T, program, dir string, env []string, args ...string) (int, string, string) {
	t.Helper()
	hostGuard(t)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, program, args...)
	cmd.Dir, cmd.Env = dir, env
	var stdout, stderr strings.Builder
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	err := cmd.Run()
	if ctx.Err() != nil {
		t.Fatal("startup did not finish", ctx.Err(), stderr.String())
	}
	if err == nil {
		return 0, stdout.String(), stderr.String()
	}
	if exit, ok := err.(*exec.ExitError); ok {
		return exit.ExitCode(), stdout.String(), stderr.String()
	}
	t.Fatal(err)
	return -1, "", ""
}

func pythonStartupEnvironment(t *testing.T, dir string) []string {
	t.Helper()
	env := environmentMap(os.Environ())
	for key := range env {
		if strings.HasPrefix(key, "FI_") || strings.HasPrefix(key, "TRACEAI_") || strings.HasPrefix(key, "PYTHON") {
			delete(env, key)
		}
	}
	env["TRACEAI_CACHE_DIR"] = filepath.Join(dir, "cache")
	env["PYTHONPATH"] = filepath.Join(dir, "project")
	env["PYTHONNOUSERSITE"], env["PYTHONDONTWRITEBYTECODE"] = "1", "1"
	put(t, filepath.Join(dir, "project/fi_instrumentation/__init__.py"), "")
	put(t, filepath.Join(dir, "project/fi_instrumentation/auto/__init__.py"), "")
	put(t, filepath.Join(dir, "project/fi_instrumentation/auto/_bootstrap.py"), "import sys\ndef start():\n    sys._launcher_test_starts = getattr(sys, '_launcher_test_starts', 0) + 1\n")
	return environmentList(env)
}

func TestPythonApplicationStartupExceptionsPreserved(t *testing.T) {
	if os.Getenv("TRACEAI_E2E") != "1" {
		t.Skip("real Python startup requires TRACEAI_E2E=1")
	}
	python := requireProgram(t, "python3")
	for _, tc := range []struct{ name, body, error string }{
		{"system_exit", "raise SystemExit(3)\n", "SystemExit: 3"},
		{"keyboard_interrupt", "raise KeyboardInterrupt('app-abort')\n", "KeyboardInterrupt: app-abort"},
		{"runtime_error", "raise RuntimeError('app-error')\n", "RuntimeError: app-error"},
		{"import_error", "raise ImportError('app-dependency')\n", "ImportError: app-dependency"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			env := pythonStartupEnvironment(t, dir)
			put(t, filepath.Join(dir, "project/sitecustomize.py"), tc.body)
			args := []string{"-c", "print('APP_RAN')"}
			code, out, stderr := startupProbe(t, python, dir, env, args...)
			plan := Prepare(nil, env, dir, Options{Runtimes: []string{"python"}})
			actualCode, actualOut, actualError := startupProbe(t, python, dir, plan.Environment, args...)
			if actualCode != code || actualOut != out || !strings.Contains(stderr, tc.error) || !strings.Contains(actualError, tc.error) {
				t.Fatalf("native=(%d,%q,%q) launcher=(%d,%q,%q)", code, out, stderr, actualCode, actualOut, actualError)
			}
		})
	}
	t.Run("loader_create_module", func(t *testing.T) {
		dir := t.TempDir()
		env := pythonStartupEnvironment(t, dir)
		put(t, filepath.Join(dir, "project/sitecustomize.py"), "VALUE = 'application'\n")
		driver := `import importlib.machinery as m, os, sys
class AppLoader(m.SourceFileLoader):
    def create_module(self, spec):
        raise SystemExit(3)
project = os.path.join(os.getcwd(), 'project')
sys.path_importer_cache[project] = m.FileFinder(project, (AppLoader, ['.py']))
import site
site.main()
print('APP_RAN')
`
		args := []string{"-S", "-c", driver}
		code, out, stderr := startupProbe(t, python, dir, env, args...)
		plan := Prepare(nil, env, dir, Options{Runtimes: []string{"python"}})
		actualCode, actualOut, actualError := startupProbe(t, python, dir, plan.Environment, args...)
		if code != 3 || actualCode != code || actualOut != out || actualError != stderr || strings.Contains(actualOut, "APP_RAN") {
			t.Fatalf("application loader failure swallowed: native=(%d,%q,%q) launcher=(%d,%q,%q)", code, out, stderr, actualCode, actualOut, actualError)
		}
	})
}

func TestPythonNestedDifferentCacheStartup(t *testing.T) {
	if os.Getenv("TRACEAI_E2E") != "1" {
		t.Skip("real Python startup requires TRACEAI_E2E=1")
	}
	python := requireProgram(t, "python3")
	for _, depth := range []int{1, 2, 3} {
		t.Run(string(rune('0'+depth)), func(t *testing.T) {
			dir := t.TempDir()
			original := pythonStartupEnvironment(t, dir)
			put(t, filepath.Join(dir, "project/sitecustomize.py"), "import os\nos.environ['APP_SITE_LOADED'] = 'yes'\nVALUE = 'app-helper'\nimport sitecustomize\nassert sitecustomize.VALUE == VALUE\n")
			env := original
			for i := 0; i < depth; i++ {
				next := environmentMap(env)
				next["TRACEAI_CACHE_DIR"] = filepath.Join(dir, "cache-"+string(rune('0'+i)))
				plan := Prepare(nil, environmentList(next), dir, Options{Runtimes: []string{"python"}})
				env = plan.Environment
			}
			code, out, stderr := startupProbe(t, python, dir, env, "-c", "import os,sys,sitecustomize; assert os.getenv('APP_SITE_LOADED') == 'yes'; assert sitecustomize.VALUE == 'app-helper'; assert sys._launcher_test_starts == 1; print('CHAINED_ONCE')")
			if code != 0 || strings.TrimSpace(out) != "CHAINED_ONCE" || stderr != "" {
				t.Fatal(code, out, stderr)
			}
		})
	}
}

func TestPythonInstrumentationStartupFailureRemainsFailOpen(t *testing.T) {
	if os.Getenv("TRACEAI_E2E") != "1" {
		t.Skip("real Python startup requires TRACEAI_E2E=1")
	}
	python := requireProgram(t, "python3")
	dir := t.TempDir()
	env := pythonStartupEnvironment(t, dir)
	put(t, filepath.Join(dir, "project/fi_instrumentation/auto/_bootstrap.py"), "def start():\n    raise SystemExit(9)\n")
	put(t, filepath.Join(dir, "project/sitecustomize.py"), "VALUE = 'application'\n")
	plan := Prepare(nil, env, dir, Options{Runtimes: []string{"python"}})
	code, out, stderr := startupProbe(t, python, dir, plan.Environment, "-c", "import sitecustomize; print(sitecustomize.VALUE)")
	if code != 0 || strings.TrimSpace(out) != "application" || stderr != "" {
		t.Fatal(code, out, stderr)
	}
}

func TestNodePnPProjectLoaderPrecedesAgent(t *testing.T) {
	if os.Getenv("TRACEAI_E2E") != "1" {
		t.Skip("offline Yarn PnP process test requires TRACEAI_E2E=1")
	}
	node, yarn := requireProgram(t, "node"), requireProgram(t, "yarn")
	dir := t.TempDir()
	put(t, filepath.Join(dir, "package.json"), `{"name":"pnp-app","version":"1.0.0","private":true,"dependencies":{"@traceai/auto":"file:./agent"}}`)
	put(t, filepath.Join(dir, "agent/package.json"), `{"name":"@traceai/auto","version":"1.0.0"}`)
	put(t, filepath.Join(dir, "agent/register.js"), "global.__launcher_pnp_init = (global.__launcher_pnp_init || 0) + 1;\n")
	env := environmentMap(os.Environ())
	env["NODE_OPTIONS"], env["TRACEAI_NODE_AGENT"] = "", "/missing-agent"
	env["TRACEAI_CACHE_DIR"] = filepath.Join(dir, "cache with spaces")
	env["YARN_CACHE_FOLDER"] = filepath.Join(dir, "yarn-cache")
	code, out, stderr := startupProbe(t, yarn, dir, environmentList(env), "install", "--offline", "--ignore-scripts", "--enable-pnp", "--no-progress", "--non-interactive", "--ignore-engines")
	if code != 0 {
		t.Fatal("offline PnP fixture failed", code, out, stderr)
	}
	pnp := filepath.Join(dir, ".pnp.js")
	if !readableFile(pnp) {
		t.Fatal("Yarn did not generate a PnP loader")
	}
	env["NODE_OPTIONS"] = "--require " + quoteNodeOption(pnp) + " --title=application"
	put(t, filepath.Join(dir, "app.cjs"), "if (global.__launcher_pnp_init !== 1) throw new Error('agent must initialize before app'); require('@traceai/auto/register'); if (global.__launcher_pnp_init !== 1) throw new Error('duplicate agent'); console.log('PNP_AGENT_ONCE');")
	original := environmentList(env)
	code, out, stderr = startupProbe(t, node, dir, original, "-r", "@traceai/auto/register", "app.cjs")
	if code != 0 || strings.TrimSpace(out) != "PNP_AGENT_ONCE" {
		t.Fatal("PnP baseline failed", code, out, stderr)
	}
	plan := Prepare(nil, original, dir, Options{Runtimes: []string{"node"}})
	nested := Prepare(nil, plan.Environment, dir, Options{Runtimes: []string{"node"}})
	if !reflect.DeepEqual(plan.Environment, nested.Environment) {
		t.Fatal("nested PnP injection changed options")
	}
	code, out, stderr = startupProbe(t, node, dir, nested.Environment, "app.cjs")
	if code != 0 || strings.TrimSpace(out) != "PNP_AGENT_ONCE" {
		t.Fatal("PnP project agent was not loaded", code, out, stderr)
	}
}
