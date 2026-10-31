package launcher

import (
	"context"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"time"
)

func installCLI(args []string, dir string) int {
	if len(args) == 0 {
		return usage("install requires a runtime")
	}
	runtime := args[0]
	f := flagSet("install")
	from := f.String("from", "", "local agent file or directory")
	address := f.String("url", "", "explicit HTTPS jar or startup-hook zip release URL")
	digest := f.String("sha256", "", "trusted SHA-256 digest required for --url")
	if err := f.Parse(args[1:]); err != nil {
		return 2
	}
	if f.NArg() != 0 {
		return usage("unexpected install arguments")
	}
	if *address != "" {
		if *from != "" || runtime != "java" && runtime != "dotnet" {
			return usage("--url requires a single java or dotnet runtime and cannot be combined with --from")
		}
		if err := installRelease(runtime, *address, *digest, os.Environ()); err != nil {
			diagnosticf("traceai: %s\n", err)
			return 1
		}
		fmt.Println("traceai: " + runtime + " release installed")
		return 0
	}
	if *digest != "" {
		return usage("--sha256 requires --url")
	}
	runtimes := []string{runtime}
	if runtime == "all" {
		if *from != "" {
			return usage("--from requires a single runtime")
		}
		runtimes = []string{"python", "node", "java", "dotnet", "go", "ruby"}
	}
	code := 0
	for _, name := range runtimes {
		switch name {
		case "python", "node", "java", "dotnet", "go", "ruby":
		default:
			return usage("unknown install runtime")
		}
		if err := Install(name, *from, dir, os.Environ()); err != nil {
			diagnosticf("traceai: %s\n", err)
			code = 1
		} else {
			fmt.Println("traceai: " + name + " agent installed")
		}
	}
	return code
}

// Install is an explicit, mutating setup operation, never called from run.
// Java/.NET/Go/Node bundle installation is offline and only accepts trusted local
// artifacts or compile-time payloads; it never executes a downloaded archive.
func Install(runtime, from, dir string, original []string) error {
	env := environmentMap(original)
	if from == "" && runtime == "python" {
		python := projectPython(dir, env)
		args := []string{"-m", "pip", "install", "fi-instrumentation-otel"}
		program := python
		if uv, err := lookProgram("uv", dir, env); err == nil && readableFile(filepath.Join(dir, "uv.lock")) {
			program = uv
			args = []string{"pip", "install", "--python", python, "fi-instrumentation-otel"}
		}
		return runInstallCommand(program, args, dir, original)
	}
	if from == "" && runtime == "node" {
		manager := packageManager(dir)
		args := []string{"add", "@traceai/auto"}
		if manager == "npm" {
			args = []string{"install", "@traceai/auto"}
		}
		return runInstallCommand(manager, args, dir, original)
	}
	if from == "" && runtime == "ruby" {
		return runInstallCommand("gem", []string{"install", "traceai-auto"}, dir, original)
	}
	cache, err := cacheDirectory(env)
	if err != nil {
		return fmt.Errorf("agent cache unavailable")
	}
	if from == "" {
		root, err := embeddedAgent(runtime, cache)
		if err != nil || root == "" {
			return fmt.Errorf("%s agent not embedded; supply --from PATH from a trusted traceAI release", runtime)
		}
		return nil
	}
	absolute, err := filepath.Abs(from)
	if err != nil {
		return fmt.Errorf("invalid agent path")
	}
	info, err := os.Lstat(absolute)
	if err != nil || info.Mode()&fs.ModeSymlink != 0 {
		return fmt.Errorf("agent source is missing or a symlink")
	}
	files := map[string][]byte{}
	if info.IsDir() {
		files, err = filesFromFS(os.DirFS(absolute), ".")
	} else {
		if !info.Mode().IsRegular() {
			return fmt.Errorf("agent source must be a regular file or directory")
		}
		if runtime != "java" {
			return fmt.Errorf("%s requires its complete agent directory, not a single file", runtime)
		}
		data, e := os.ReadFile(absolute)
		err = e
		files[agentEntry(runtime)] = data
	}
	if err != nil {
		return fmt.Errorf("cannot read agent source")
	}
	if _, err = installFiles(cache, runtime, files); err != nil {
		return err
	}
	return nil
}
func runInstallCommand(program string, args []string, dir string, env []string) error {
	cmd := exec.Command(program, args...)
	cmd.Dir = dir
	cmd.Env = env
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("%s installation command failed", filepath.Base(program))
	}
	return nil
}

func setupCLI(args []string, dir string) int {
	if len(args) == 0 || args[0] != "go" || len(args) > 2 {
		return usage("setup go [dir]")
	}
	if len(args) == 2 {
		dir = args[1]
		if !filepath.IsAbs(dir) {
			cwd, _ := os.Getwd()
			dir = filepath.Join(cwd, dir)
		}
	}
	root := moduleRoot(dir)
	if root == "" {
		diagnosticf("traceai: setup go requires a module\n")
		return 1
	}
	env := environmentMap(os.Environ())
	program, err := lookProgram("go", dir, env)
	if err != nil {
		diagnosticf("traceai: Go toolchain not found\n")
		return 1
	}
	packages, err := listedGoPackages(program, dir, env, []string{"./..."})
	if err != nil {
		diagnosticf("traceai: cannot inspect Go packages\n")
		return 1
	}
	type added struct{ path string }
	var written []added
	rollback := func() {
		for _, entry := range written {
			_ = os.Remove(entry.path)
		}
	}
	for _, pkg := range packages {
		if pkg.Name != "main" || pkg.Module == nil || filepath.Clean(pkg.Module.Dir) != filepath.Clean(root) {
			continue
		}
		has := false
		for _, imp := range pkg.Imports {
			if imp == agentModule {
				has = true
			}
		}
		if has {
			continue
		}
		path := filepath.Join(pkg.Dir, "traceai_auto.go")
		file, e := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0644)
		if e != nil {
			rollback()
			diagnosticf("traceai: traceai_auto.go already exists or cannot be written; not overwritten\n")
			return 1
		}
		_, e = file.WriteString("// traceAI auto-instrumentation. Remove this file to opt out.\npackage main\n\nimport _ \"" + agentModule + "\"\n")
		closeErr := file.Close()
		written = append(written, added{path})
		if e != nil || closeErr != nil {
			rollback()
			diagnosticf("traceai: cannot write Go import file\n")
			return 1
		}
	}
	if len(written) == 0 {
		fmt.Println("traceai: no uninstrumented main packages found")
		return 0
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	cmd := exec.CommandContext(ctx, program, "get", agentModule)
	cmd.Dir = root
	cmd.Env = os.Environ()
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		rollback()
		diagnosticf("traceai: go get failed; generated import files removed (inspect go.mod/go.sum for toolchain changes)\n")
		return 1
	}
	fmt.Printf("traceai: instrumented %d main package(s); commit the import files and go.mod/go.sum changes\n", len(written))
	return 0
}
