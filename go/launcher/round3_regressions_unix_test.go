//go:build !windows

package launcher

import (
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestGoGraphProbeFailuresRemainFailOpen(t *testing.T) {
	for _, mode := range []string{"baseline_failure", "workspace_failure", "empty", "upgrade", "redirect", "agent_redirect"} {
		t.Run(mode, func(t *testing.T) {
			dir, agent, env := fakeGoProject(t)
			pkg := fmt.Sprintf(`{"Dir":%q,"Name":"main","Module":{"Path":"example.com/app","Dir":%q,"GoVersion":"1.26.0"}}`, dir, dir)
			baseline := `{"Path":"example.com/app"}{"Path":"shared","Version":"v1.0.0","Replace":{"Path":"/old","Dir":"/old"}}`
			injected := baseline
			switch mode {
			case "upgrade":
				injected = strings.ReplaceAll(baseline, "v1.0.0", "v1.1.0")
			case "redirect":
				injected = strings.ReplaceAll(baseline, "/old", "/new")
			case "agent_redirect":
				baseline += fmt.Sprintf(`{"Path":%q,"Version":"v0.1.0","Dir":"/pinned-agent"}`, agentModule)
				injected += fmt.Sprintf(`{"Path":%q,"Dir":"/injected-agent"}`, agentModule)
			case "empty":
				injected = ""
			}
			body := "if test \"$2\" != -m; then printf '%s' " + shellQuote(pkg) + "; exit; fi\n"
			if mode == "baseline_failure" {
				body += "exit 1\n"
			} else {
				body += "if test -z \"$GOWORK\"; then printf '%s' " + shellQuote(baseline) + "; else "
				if mode == "workspace_failure" {
					body += "exit 1"
				} else {
					body += "printf '%s' " + shellQuote(injected)
				}
				body += "; fi\n"
			}
			executable(t, filepath.Join(env["PATH"], "go"), body)
			before := environmentList(env)
			warning := prepareGo([]string{"go", "run", "."}, dir, filepath.Join(t.TempDir(), "cache"), agent, env, func(k, v string) { env[k] = v })
			if !strings.Contains(warning, "dependency selections") || !reflect.DeepEqual(before, environmentList(env)) {
				t.Fatal("unsafe graph activated", warning, env)
			}
		})
	}
}

func TestCacheRepairDoesNotFollowExtraSymlinks(t *testing.T) {
	cache := filepath.Join(t.TempDir(), "cache")
	files := map[string][]byte{"preload.cjs": []byte("original")}
	root, err := installFiles(cache, "node", files)
	if err != nil {
		t.Fatal(err)
	}
	external := t.TempDir()
	put(t, filepath.Join(external, "sentinel"), "untouched")
	if err := os.Symlink(external, filepath.Join(root, "extra")); err != nil {
		t.Fatal(err)
	}
	if _, err := installFiles(cache, "node", files); err != nil {
		t.Fatal(err)
	}
	if data, err := os.ReadFile(filepath.Join(external, "sentinel")); err != nil || string(data) != "untouched" {
		t.Fatal("followed extra symlink", err)
	}
	if !validPayload(root) {
		t.Fatal("failed to repair extra symlink")
	}
}

func TestPathSearchUsesCallerExecutePermission(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root can execute any file with an execute bit")
	}
	first, second := t.TempDir(), t.TempDir()
	bad, good := filepath.Join(first, "verify-tool"), filepath.Join(second, "verify-tool")
	put(t, bad, "#!/bin/sh\necho BAD\n")
	if err := os.Chmod(bad, 0641); err != nil {
		t.Fatal(err)
	}
	executable(t, good, "echo GOOD")
	env := map[string]string{"PATH": first + string(os.PathListSeparator) + second}
	found, err := lookProgram("verify-tool", t.TempDir(), env)
	if err != nil || found != good {
		t.Fatal("selected a file the caller cannot execute", found, err)
	}
	if _, err := lookProgram(bad, t.TempDir(), env); err != errNotExecutable {
		t.Fatal("explicit denied path accepted", err)
	}
}

func TestPythonInstalledPthCannotBypassFailOpenShim(t *testing.T) {
	if os.Getenv("TRACEAI_E2E") != "1" {
		t.Skip("real Python .pth startup requires TRACEAI_E2E=1")
	}
	python := requireProgram(t, "python3")
	dir := t.TempDir()
	env := pythonStartupEnvironment(t, dir)
	venv := filepath.Join(dir, "venv")
	code, out, stderr := startupProbe(t, python, dir, env, "-m", "venv", "--without-pip", venv)
	if code != 0 {
		t.Fatal(code, out, stderr)
	}
	venvPython := filepath.Join(venv, "bin/python")
	code, out, stderr = startupProbe(t, venvPython, dir, env, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))")
	if code != 0 {
		t.Fatal(code, out, stderr)
	}
	sitePackages := strings.TrimSpace(out)
	pth, err := os.ReadFile(filepath.Join(repoRoot(t), "python/fi_instrumentation_auto.pth"))
	if err != nil {
		t.Fatal(err)
	}
	put(t, filepath.Join(sitePackages, "fi_instrumentation_auto.pth"), string(pth))
	put(t, filepath.Join(dir, "project/fi_instrumentation/auto/_bootstrap.py"), "def start():\n    raise SystemExit(9)\n")
	put(t, filepath.Join(dir, "project/sitecustomize.py"), "VALUE = 'app-site'\n")
	args := []string{"-c", "import sitecustomize; assert sitecustomize.VALUE == 'app-site'; print('APP_RAN')"}
	code, out, stderr = startupProbe(t, venvPython, dir, env, args...)
	if code != 0 || strings.TrimSpace(out) != "APP_RAN" || stderr != "" {
		t.Fatal("native startup failed", code, out, stderr)
	}
	plan := Prepare(nil, env, dir, Options{Runtimes: []string{"python"}})
	actualCode, actualOut, actualErr := startupProbe(t, venvPython, dir, plan.Environment, args...)
	if actualCode != code || actualOut != out || actualErr != stderr {
		t.Fatal(".pth escaped fail-open guard", actualCode, actualOut, actualErr)
	}
}

func TestPythonChainsMetaPathSitecustomize(t *testing.T) {
	if os.Getenv("TRACEAI_E2E") != "1" {
		t.Skip("real Python meta-path startup requires TRACEAI_E2E=1")
	}
	python := requireProgram(t, "python3")
	for _, mode := range []string{"virtual", "finder_failure", "module_failure"} {
		t.Run(mode, func(t *testing.T) {
			dir := t.TempDir()
			env := pythonStartupEnvironment(t, dir)
			driver := fmt.Sprintf(`import importlib.abc, importlib.util, os, site, sys
# Homebrew ships its own sitecustomize in the stdlib directory. This virtual
# application fixture has no filesystem sitecustomize except the injected shim.
root = os.path.realpath(os.getcwd()) + os.sep
sys.path[:] = [p for p in sys.path if not os.path.isfile(os.path.join(p, 'sitecustomize.py')) or os.path.realpath(p).startswith(root)]
mode = %q
class AppLoader(importlib.abc.Loader):
    def create_module(self, spec): return None
    def exec_module(self, module):
        if mode == 'module_failure': raise SystemExit(7)
        module.VALUE = 'application'
        sys.APP_SITE_LOADED = 'yes'
class AppFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'sitecustomize':
            if mode == 'finder_failure': raise SystemExit(6)
            return importlib.util.spec_from_loader(fullname, AppLoader(), origin=None)
sys.meta_path.append(AppFinder())
import site
# Exercise Python's actual sitecustomize importer without loading unrelated
# machine-global .pth/sitecustomize packages into this offline fixture.
site.execsitecustomize()
import sitecustomize
assert getattr(sitecustomize, 'VALUE', None) == 'application', repr(sitecustomize.__spec__)
print('APP_SITE_LOADED=' + sys.APP_SITE_LOADED)
`, mode)
			args := []string{"-S", "-c", driver}
			code, out, stderr := startupProbe(t, python, dir, env, args...)
			plan := Prepare(nil, env, dir, Options{Runtimes: []string{"python"}})
			actualCode, actualOut, actualErr := startupProbe(t, python, dir, plan.Environment, args...)
			if actualCode != code || actualOut != out || actualErr != stderr || mode == "virtual" && strings.TrimSpace(out) != "APP_SITE_LOADED=yes" {
				t.Fatalf("native=(%d,%q,%q) launcher=(%d,%q,%q)", code, out, stderr, actualCode, actualOut, actualErr)
			}
		})
	}
}

func TestNodeCommandLinePnPLoaderPrecedesAgent(t *testing.T) {
	if os.Getenv("TRACEAI_E2E") != "1" {
		t.Skip("real Node CLI-loader startup requires TRACEAI_E2E=1")
	}
	node, yarn := requireProgram(t, "node"), requireProgram(t, "yarn")
	dir := t.TempDir()
	put(t, filepath.Join(dir, "package.json"), `{"name":"cli-pnp-app","version":"1.0.0","private":true,"dependencies":{"@traceai/auto":"file:./agent"}}`)
	put(t, filepath.Join(dir, "agent/package.json"), `{"name":"@traceai/auto","version":"1.0.0"}`)
	put(t, filepath.Join(dir, "agent/register.js"), "global.CLI_AGENT = (global.CLI_AGENT || 0) + 1;\n")
	put(t, filepath.Join(dir, "app.cjs"), "if (global.CLI_AGENT !== 1) throw new Error('agent must run before app'); console.log('AGENT=1');")
	put(t, filepath.Join(dir, "last.cjs"), "if (global.CLI_AGENT !== 1) throw new Error('ordinary preload ran before agent'); global.LAST_PRELOAD = true;")
	env := environmentMap(os.Environ())
	env["NODE_OPTIONS"], env["TRACEAI_NODE_AGENT"] = "", "/missing-agent"
	env["TRACEAI_CACHE_DIR"], env["YARN_CACHE_FOLDER"] = filepath.Join(dir, "cache"), filepath.Join(dir, "yarn-cache")
	code, out, stderr := startupProbe(t, yarn, dir, environmentList(env), "install", "--offline", "--ignore-scripts", "--enable-pnp", "--no-progress", "--non-interactive", "--ignore-engines")
	if code != 0 {
		t.Fatal(code, out, stderr)
	}
	code, out, stderr = startupProbe(t, node, dir, environmentList(env), "-r", "./.pnp.js", "-r", "@traceai/auto/register", "app.cjs")
	if code != 0 || strings.TrimSpace(out) != "AGENT=1" {
		t.Fatal("native PnP baseline failed", code, out, stderr)
	}
	for _, args := range [][]string{
		{"-r", "./.pnp.js", "app.cjs"},
		{"--require=./.pnp.js", "--require=./last.cjs", "app.cjs"},
		{"-r", "./.pnp.js", "-r", "@traceai/auto/register", "app.cjs"},
		{"-r", "./.pnp.js", "-e", "if (global.CLI_AGENT !== 1) throw new Error('eval too early'); console.log('AGENT=1')"},
	} {
		t.Run(strings.Join(args[:2], " "), func(t *testing.T) {
			plan := Prepare(append([]string{node}, args...), environmentList(env), dir, Options{Runtimes: []string{"node"}})
			code, out, stderr := startupProbe(t, node, dir, plan.Environment, args...)
			if code != 0 || strings.TrimSpace(out) != "AGENT=1" {
				t.Fatal("CLI loader ran too late", code, out, stderr)
			}
		})
	}
}

func TestGoWorkspaceCannotUpgradeAppDependencies(t *testing.T) {
	if os.Getenv("TRACEAI_E2E") != "1" {
		t.Skip("real Go MVS checks require TRACEAI_E2E=1")
	}
	for _, compatible := range []bool{false, true} {
		t.Run(fmt.Sprint(compatible), func(t *testing.T) {
			physicalDir, agent, v1, v2 := t.TempDir(), t.TempDir(), t.TempDir(), t.TempDir()
			dir := filepath.Join(t.TempDir(), "symlink-spelled-app")
			if err := os.Symlink(physicalDir, dir); err != nil {
				t.Fatal(err)
			}
			for path, value := range map[string]string{v1: "APP_PINNED_V1", v2: "AGENT_SELECTED_V2"} {
				put(t, filepath.Join(path, "go.mod"), "module example.com/shared\n\ngo 1.26.0\n")
				put(t, filepath.Join(path, "shared.go"), "package shared\nconst Value="+fmt.Sprintf("%q", value)+"\n")
			}
			mod := fmt.Sprintf("module example.com/mvs-app\n\ngo 1.26.0\nrequire example.com/shared v1.0.0\nreplace example.com/shared v1.0.0 => %s\nreplace example.com/shared v1.1.0 => %s\n", v1, v2)
			put(t, filepath.Join(dir, "go.mod"), mod)
			put(t, filepath.Join(dir, "go.sum"), "")
			put(t, filepath.Join(dir, "main.go"), "package main\nimport(\"fmt\";\"example.com/shared\")\nfunc main(){fmt.Println(shared.Value)}\n")
			version := "v1.1.0"
			if compatible {
				version = "v1.0.0"
			}
			put(t, filepath.Join(agent, "go.mod"), "module "+agentModule+"\n\ngo 1.26.0\nrequire example.com/shared "+version+"\n")
			put(t, filepath.Join(agent, "auto.go"), "package auto\nimport _ \"example.com/shared\"\n")
			env := environmentMap(os.Environ())
			env["GOFLAGS"], env["GOWORK"], env["GOPROXY"] = "-p=2", "", "off"
			original := environmentList(env)
			baseline := runRegressionGo(t, dir, original, "run", ".")
			warning := prepareGo([]string{"go", "run", "."}, dir, filepath.Join(t.TempDir(), "cache"), agent, env, func(k, v string) { env[k] = v })
			if compatible {
				if warning != "" || env["GOWORK"] == "" {
					t.Fatal("compatible graph rejected", warning)
				}
			} else if failure := workspacePreservationFailure(warning, original, env); failure != "" {
				t.Fatal(failure)
			}
			after := runRegressionGo(t, dir, environmentList(env), "run", ".")
			if baseline != "APP_PINNED_V1\n" || after != baseline {
				t.Fatal("application behavior changed", baseline, after)
			}
			for file, want := range map[string]string{"go.mod": mod, "go.sum": ""} {
				if data, _ := os.ReadFile(filepath.Join(dir, file)); string(data) != want {
					t.Fatal("application module files changed", file)
				}
			}
		})
	}
}

func TestFullDotnetHookSupports8And10(t *testing.T) {
	if os.Getenv("TRACEAI_E2E") != "1" || Flavour != "full" {
		t.Skip("full .NET runtime checks require TRACEAI_E2E=1 and embedagents")
	}
	root := repoRoot(t)
	dotnet := requireProgram(t, filepath.Join(root, "csharp/.dotnet/dotnet"))
	for _, tfm := range []string{"net8.0", "net10.0"} {
		t.Run(tfm, func(t *testing.T) {
			app := filepath.Join(root, "csharp/tests/TraceAI.AutoInstrumentation.TestApp/bin/Release", tfm, "TraceAI.AutoInstrumentation.TestApp.dll")
			if !readableFile(app) {
				t.Fatal("required prebuilt runtime probe absent", app)
			}
			dir := t.TempDir()
			env := environmentMap(os.Environ())
			delete(env, "TRACEAI_DOTNET_AGENT")
			delete(env, "DOTNET_STARTUP_HOOKS")
			env["DOTNET_ROOT"], env["OTEL_SDK_DISABLED"], env["TRACEAI_CACHE_DIR"] = filepath.Join(root, "csharp/.dotnet"), "true", filepath.Join(dir, "cache")
			original := environmentList(env)
			code, out, stderr := startupProbe(t, dotnet, dir, original, app, "idle")
			if code != 0 {
				t.Fatal("native .NET failed", code, out, stderr)
			}
			plan := Prepare(nil, original, dir, Options{Runtimes: []string{"dotnet"}})
			if environmentMap(plan.Environment)["DOTNET_STARTUP_HOOKS"] == "" {
				t.Fatal("no embedded hook injected", plan.Warnings)
			}
			actualCode, actualOut, actualErr := startupProbe(t, dotnet, dir, plan.Environment, app, "idle")
			if actualCode != code || actualOut != out || actualErr != stderr {
				t.Fatal("embedded hook changed .NET app behavior", actualCode, actualOut, actualErr)
			}
		})
	}
}
