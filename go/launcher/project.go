package launcher

import (
	"context"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// DetectProject reports languages in stable order using project manifests only;
// it never executes application code or walks dependencies.
func DetectProject(dir string) []string {
	patterns := []struct {
		name  string
		globs []string
	}{
		{"python", []string{"pyproject.toml", "requirements*.txt", "Pipfile", "setup.py"}},
		{"node", []string{"package.json"}},
		{"java", []string{"pom.xml", "build.gradle*"}},
		{"dotnet", []string{"*.csproj", "*.fsproj", "*.sln", "*.slnx"}},
		{"go", []string{"go.mod"}},
		{"ruby", []string{"Gemfile", "*.gemspec"}},
	}
	var result []string
	for _, entry := range patterns {
		for _, pattern := range entry.globs {
			matches, _ := filepath.Glob(filepath.Join(dir, pattern))
			if len(matches) > 0 {
				result = append(result, entry.name)
				break
			}
		}
	}
	return result
}
func projectPython(dir string, env map[string]string) string {
	if explicit := env["TRACEAI_PYTHON"]; explicit != "" {
		return explicit
	}
	candidates := []string{}
	if env["VIRTUAL_ENV"] != "" {
		candidates = append(candidates, filepath.Join(env["VIRTUAL_ENV"], "bin", "python"), filepath.Join(env["VIRTUAL_ENV"], "Scripts", "python.exe"))
	}
	candidates = append(candidates, filepath.Join(dir, ".venv", "bin", "python"), filepath.Join(dir, "venv", "bin", "python"), filepath.Join(dir, ".venv", "Scripts", "python.exe"))
	for _, candidate := range candidates {
		if readableFile(candidate) {
			return candidate
		}
	}
	for _, name := range []string{"python3", "python"} {
		if path, err := lookProgram(name, dir, env); err == nil {
			return path
		}
	}
	return "python3"
}
func packageManager(dir string) string {
	for _, entry := range []struct{ file, name string }{{"pnpm-lock.yaml", "pnpm"}, {"yarn.lock", "yarn"}, {"bun.lock", "bun"}, {"bun.lockb", "bun"}, {"package-lock.json", "npm"}, {"npm-shrinkwrap.json", "npm"}} {
		if readableFile(filepath.Join(dir, entry.file)) {
			return entry.name
		}
	}
	return "npm"
}

type RuntimeStatus struct {
	Runtime   string `json:"runtime"`
	Available bool   `json:"available"`
	Detail    string `json:"detail"`
}

func RuntimeAvailability(dir string, original []string) []RuntimeStatus {
	env := environmentMap(original)
	cache, _ := cacheDirectory(env)
	agents := discoverAgents(cache, env)
	statuses := []RuntimeStatus{}
	// Diagnostics are explicit, time-bounded, and do not initialize an agent.
	python := projectPython(dir, env)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	cmd := exec.CommandContext(ctx, python, "-c", "import importlib.util; import sys; sys.exit(0 if importlib.util.find_spec('fi_instrumentation') else 1)")
	cmd.Dir = dir
	cmd.WaitDelay = 100 * time.Millisecond
	child := environmentMap(original)
	delete(child, "FI_AUTO_INSTRUMENT")
	delete(child, "PYTHONPATH")
	cmd.Env = environmentList(child)
	ok := cmd.Run() == nil
	cancel()
	detail := "not installed in " + python + "; traceai install python"
	if ok {
		detail = python
	}
	statuses = append(statuses, RuntimeStatus{"python", ok, detail})
	nodeOK := agents.Node != ""
	nodeDetail := agents.Node
	ctx, cancel = context.WithTimeout(context.Background(), 3*time.Second)
	node := exec.CommandContext(ctx, "node", "-p", "require.resolve('@traceai/auto/register', {paths: [process.cwd()]})")
	node.Dir = dir
	node.WaitDelay = 100 * time.Millisecond
	child = environmentMap(original)
	delete(child, "NODE_OPTIONS")
	node.Env = environmentList(child)
	output, err := node.Output()
	cancel()
	if err == nil {
		nodeOK = true
		nodeDetail = strings.TrimSpace(string(output))
	}
	if !nodeOK {
		nodeDetail = "not installed; traceai install node"
	}
	statuses = append(statuses, RuntimeStatus{"node", nodeOK, nodeDetail})
	rubyOK := agents.Ruby != ""
	rubyDetail := agents.Ruby
	ctx, cancel = context.WithTimeout(context.Background(), 3*time.Second)
	ruby := exec.CommandContext(ctx, "ruby", "-e", "require 'rubygems'; print Gem::Specification.find_by_name('traceai-auto').full_gem_path")
	ruby.Dir = dir
	ruby.WaitDelay = 100 * time.Millisecond
	child = environmentMap(original)
	delete(child, "RUBYOPT") // Diagnostics must not initialize the agent.
	ruby.Env = environmentList(child)
	output, err = ruby.Output()
	cancel()
	if err == nil {
		rubyOK, rubyDetail = true, strings.TrimSpace(string(output))
	}
	if !rubyOK {
		rubyDetail = "gem not installed and fallback not cached; traceai install ruby"
	}
	statuses = append(statuses, RuntimeStatus{"ruby", rubyOK, rubyDetail})
	for _, entry := range []struct{ name, path string }{{"java", agents.Java}, {"dotnet", agents.Dotnet}, {"go", agents.Go}} {
		detail := entry.path
		if detail == "" {
			detail = "agent not cached; install with --from or use a full build"
		}
		statuses = append(statuses, RuntimeStatus{entry.name, entry.path != "", detail})
	}
	for i := range statuses {
		statuses[i].Detail = displayed(map[string]string{"detail": statuses[i].Detail}, original)["detail"]
	}
	return statuses
}
