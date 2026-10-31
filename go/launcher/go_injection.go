package launcher

import (
	"bytes"
	"context"
	"debug/buildinfo"
	"encoding/json"
	"fmt"
	goversion "go/version"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

type goPackage struct {
	Dir          string
	Name         string
	Imports      []string
	Deps         []string
	TestImports  []string
	XTestImports []string
	Module       *struct{ Path, Dir, GoVersion string }
}

func packageHasAgent(pkg goPackage) bool {
	// A project-owned agent (including a transitive dependency) owns its version
	// and initialization. Do not replace it with our workspace source snapshot.
	for _, imports := range [][]string{pkg.Imports, pkg.Deps} {
		for _, imp := range imports {
			if imp == agentModule {
				return true
			}
		}
	}
	return false
}

func linkedGoAgent(program string) (isGo, traced bool) {
	info, err := buildinfo.ReadFile(program)
	if err != nil {
		return false, false
	}
	if info.Main.Path == agentModule {
		return true, true
	}
	for _, dep := range info.Deps {
		if dep.Path == agentModule {
			return true, true
		}
	}
	return true, false
}

var goFlagValues = map[string]bool{"-C": true, "-o": true, "-p": true, "-asmflags": true, "-buildmode": true, "-compiler": true, "-gccgoflags": true, "-gcflags": true, "-ldflags": true, "-mod": true, "-modfile": true, "-overlay": true, "-pkgdir": true, "-tags": true, "-toolexec": true, "-coverpkg": true, "-coverprofile": true, "-cpuprofile": true, "-memprofile": true, "-blockprofile": true, "-mutexprofile": true, "-trace": true, "-outputdir": true, "-exec": true, "-run": true, "-bench": true, "-benchtime": true, "-timeout": true, "-count": true, "-parallel": true, "-cpu": true, "-vet": true, "-shuffle": true, "-fuzz": true, "-fuzztime": true, "-fuzzminimizetime": true, "-list": true, "-skip": true, "-covermode": true, "-pgo": true, "-buildid": true}

// goTargets strips build flags, never program/test-binary arguments. Unfamiliar
// flags are left to Go; a failed discovery simply disables auto-injection.
func goTargets(args []string, sub string) ([]string, error) {
	var patterns []string
	flags := true
	for i := 0; i < len(args); i++ {
		arg := args[i]
		if flags && (arg == "-args" || arg == "--" && sub == "test") {
			break
		}
		if flags && arg == "--" {
			flags = false
			continue
		}
		if flags && strings.HasPrefix(arg, "-") {
			key, _, equal := strings.Cut(arg, "=")
			if goFlagValues[key] && !equal {
				i++
				if i >= len(args) {
					return nil, fmt.Errorf("missing Go flag value")
				}
			}
			continue
		}
		if strings.HasSuffix(arg, ".go") {
			return nil, fmt.Errorf("explicit .go files need traceai setup go or a package target")
		}
		if strings.Contains(arg, "@") {
			return nil, fmt.Errorf("versioned install targets cannot use a workspace overlay")
		}
		patterns = append(patterns, arg)
		// go run takes exactly one package; the rest belongs to the application.
		if sub == "run" {
			break
		}
	}
	if len(patterns) == 0 {
		patterns = []string{"."}
	}
	return patterns, nil
}

// goBuildFlags excludes application arguments for run and test-binary arguments
// after -args/--, but includes build/test flags following package arguments.
func goBuildFlags(args []string, sub string) []string {
	var flags []string
	for i := 0; i < len(args); i++ {
		arg := args[i]
		if arg == "-args" || arg == "--" {
			break
		}
		if !strings.HasPrefix(arg, "-") {
			if sub == "run" {
				break
			}
			continue
		}
		flags = append(flags, arg)
		key, _, equal := strings.Cut(arg, "=")
		if goFlagValues[key] && !equal && i+1 < len(args) {
			i++
			flags = append(flags, args[i])
		}
	}
	return flags
}

// goOptionWords follows cmd/internal/quoted.Split: only leading quotes group a
// field, and backslashes are ALWAYS literal (not shell/Go string escapes).
func goOptionWords(value string) ([]string, error) {
	var words []string
	space := func(c byte) bool { return c == ' ' || c == '\t' || c == '\r' || c == '\n' }
	for len(value) > 0 {
		for len(value) > 0 && space(value[0]) {
			value = value[1:]
		}
		if value == "" {
			break
		}
		if value[0] == '\'' || value[0] == '"' {
			quote := value[0]
			value = value[1:]
			end := strings.IndexByte(value, quote)
			if end < 0 {
				return nil, fmt.Errorf("unclosed GOFLAGS quote")
			}
			words = append(words, value[:end])
			value = value[end+1:]
			continue
		}
		end := 0
		for end < len(value) && !space(value[end]) {
			end++
		}
		words = append(words, value[:end])
		value = value[end:]
	}
	return words, nil
}

func quoteGoFlag(flag string) (string, error) {
	if !strings.ContainsAny(flag, " \t\r\n") {
		return flag, nil // Internal quotes are literal in an unquoted Go field.
	}
	for _, quote := range []string{"'", "\""} {
		if !strings.Contains(flag, quote) {
			return quote + flag + quote, nil
		}
	}
	return "", fmt.Errorf("Go overlay path cannot be quoted")
}

func workspaceCompatibleFlags(args []string) bool {
	for i := 0; i < len(args); i++ {
		if args[i] == "-args" || args[i] == "--" {
			break
		}
		if !strings.HasPrefix(args[i], "-") {
			continue
		}
		key, value, equal := strings.Cut(args[i], "=")
		if key == "-modfile" {
			return false
		}
		if !equal && goFlagValues[key] && i+1 < len(args) {
			i++
			value = args[i]
		}
		if key == "-mod" && value != "readonly" {
			return false
		}
	}
	return true
}

func moduleRoot(dir string) string {
	for {
		if readableFile(filepath.Join(dir, "go.mod")) {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return ""
		}
		dir = parent
	}
}
func goCommand(argv []string, dir string) (sub string, rest []string, working string) {
	working = dir
	if len(argv) < 2 {
		return
	}
	i := 1
	if argv[i] == "-C" && len(argv) > i+1 {
		working = argv[i+1]
		i += 2
	} else if strings.HasPrefix(argv[i], "-C=") {
		working = strings.TrimPrefix(argv[i], "-C=")
		i++
	}
	if !filepath.IsAbs(working) {
		working = filepath.Join(dir, working)
	}
	if i < len(argv) {
		sub = argv[i]
		rest = argv[i+1:]
	}
	return
}

type discoveryBuffer struct{ bytes.Buffer }

func (b *discoveryBuffer) Write(data []byte) (int, error) {
	if b.Len()+len(data) > 8<<20 {
		return 0, fmt.Errorf("Go discovery exceeded 8 MiB")
	}
	return b.Buffer.Write(data)
}

func configuredGoFlags(program, dir string, env map[string]string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, program, "env", "-json", "GOFLAGS")
	cmd.Dir, cmd.Env = dir, environmentList(env)
	cmd.WaitDelay = 100 * time.Millisecond
	var stdout discoveryBuffer
	cmd.Stdout, cmd.Stderr = &stdout, io.Discard
	if err := cmd.Run(); err != nil {
		return "", err
	}
	var config map[string]string
	if err := json.Unmarshal(stdout.Bytes(), &config); err != nil {
		return "", err
	}
	flags, ok := config["GOFLAGS"]
	if !ok {
		return "", fmt.Errorf("Go configuration omitted GOFLAGS")
	}
	return flags, nil
}

func listedGoPackages(program, dir string, env map[string]string, patterns []string) ([]goPackage, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	// Discovery must never resolve missing imports by editing go.mod/go.sum.
	args := append([]string{"list", "-e", "-json", "-mod=readonly"}, patterns...)
	cmd := exec.CommandContext(ctx, program, args...)
	cmd.Dir = dir
	cmd.Env = environmentList(env)
	cmd.WaitDelay = 100 * time.Millisecond
	var stdout discoveryBuffer
	cmd.Stdout = &stdout
	cmd.Stderr = io.Discard
	if err := cmd.Run(); err != nil {
		return nil, fmt.Errorf("Go package discovery failed")
	}
	decoder := json.NewDecoder(&stdout)
	var packages []goPackage
	for {
		var pkg goPackage
		err := decoder.Decode(&pkg)
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, err
		}
		packages = append(packages, pkg)
	}
	return packages, nil
}

type goModuleSelection struct {
	Path, Version, Dir string
	Replace            *struct{ Path, Version, Dir string }
	Error              *struct{ Err string }
}

// Compare the complete selected graph, not just currently imported packages:
// the app can use test/build-tag dependencies and the agent must not upgrade or
// redirect any of them through workspace MVS or replacement directives.
func selectedGoModules(program, dir string, env map[string]string) (map[string]string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, program, "list", "-m", "-json", "-mod=readonly", "all")
	cmd.Dir, cmd.Env = dir, environmentList(env)
	cmd.WaitDelay = 100 * time.Millisecond
	var stdout discoveryBuffer
	cmd.Stdout, cmd.Stderr = &stdout, io.Discard
	if err := cmd.Run(); err != nil {
		return nil, err
	}
	modules := map[string]string{}
	decoder := json.NewDecoder(&stdout)
	for {
		var mod goModuleSelection
		err := decoder.Decode(&mod)
		if err == io.EOF {
			break
		}
		if err != nil || mod.Path == "" || mod.Error != nil {
			return nil, fmt.Errorf("could not inspect Go dependency selections")
		}
		if mod.Path == agentModule {
			// A test-only/project-owned agent is a dependency selection too.
			// Main-module promotion is safe only for the same source directory;
			// never silently replace a pinned agent with our cached snapshot.
			if mod.Dir == "" {
				return nil, fmt.Errorf("unresolved Go agent selection")
			}
			modules[mod.Path] = "agent-source\x00" + filepath.Clean(mod.Dir)
			continue
		}
		selection := mod.Version
		if mod.Replace != nil {
			path := mod.Replace.Path
			if mod.Replace.Version == "" {
				path = mod.Replace.Dir
				if path == "" {
					return nil, fmt.Errorf("unresolved Go replacement")
				}
				path = filepath.Clean(path)
			}
			selection += "\x00" + path + "\x00" + mod.Replace.Version
		}
		modules[mod.Path] = selection
	}
	if len(modules) == 0 {
		return nil, fmt.Errorf("empty Go dependency selections")
	}
	return modules, nil
}

func preservedGoModules(original, injected map[string]string) bool {
	for path, selection := range original {
		if actual, ok := injected[path]; !ok || actual != selection {
			return false
		}
	}
	return true
}

// inspectGo is the default, non-invasive path: inspect executable build info
// only. Never launch the Go toolchain to discover a source project's packages.
func inspectGo(argv []string, dir string, env map[string]string) string {
	base := strings.TrimSuffix(strings.ToLower(filepath.Base(argv[0])), ".exe")
	if base == "go" {
		sub, _, _ := goCommand(argv, dir)
		if sub == "run" || sub == "build" || sub == "test" || sub == "install" {
			return "Go build injection is disabled; use traceai setup go (or experimental --go-inject / TRACEAI_GO_INJECT=1)"
		}
		return ""
	}
	program, err := lookProgram(argv[0], dir, env)
	if err == nil {
		if isGo, traced := linkedGoAgent(program); isGo && !traced {
			return "Go binary has no traceAI agent; use traceai setup go before building"
		}
	}
	return ""
}

// prepareGo implements experimental injection. Prepare gates this path on an
// explicit flag/environment opt-in; direct tests exercise the mechanism itself.
func prepareGo(argv []string, dir, cache, agent string, env map[string]string, set func(string, string)) string {
	if strings.TrimSuffix(strings.ToLower(filepath.Base(argv[0])), ".exe") != "go" {
		return inspectGo(argv, dir, env)
	}
	sub, args, working := goCommand(argv, dir)
	if sub != "run" && sub != "build" && sub != "test" && sub != "install" {
		return ""
	}
	if env["GOWORK"] != "" {
		return ""
	}
	program, err := lookProgram(argv[0], dir, env)
	if err != nil {
		return ""
	}
	// Empty (including explicitly empty) GOFLAGS falls back to the GOENV file.
	// Discovery and the actual build must use the same effective configuration.
	goFlags := env["GOFLAGS"]
	if goFlags == "" {
		goFlags, err = configuredGoFlags(program, working, env)
		if err != nil {
			return "could not inspect Go configuration; running without Go injection"
		}
	}
	flags, flagErr := goOptionWords(goFlags)
	if flagErr != nil {
		return "GOFLAGS could not be parsed; running without Go injection"
	}
	buildFlags := goBuildFlags(args, sub)
	if !workspaceCompatibleFlags(flags) || !workspaceCompatibleFlags(buildFlags) {
		return "Go module flags are incompatible with an injected workspace; use traceai setup go"
	}
	root := moduleRoot(working)
	if root == "" {
		return "Go auto-injection requires a module; use traceai setup go"
	}
	// go list reports physical module directories, including when TMPDIR or
	// the application's working directory is spelled through a symlink.
	if physical, err := filepath.EvalSymlinks(root); err == nil {
		root = physical
	}
	// Honor auto-discovered workspaces too; never replace a user's workspace.
	for d := working; ; d = filepath.Dir(d) {
		if readableFile(filepath.Join(d, "go.work")) {
			return "existing Go workspace preserved; use traceai setup go for instrumentation"
		}
		if filepath.Dir(d) == d {
			break
		}
	}
	// A workspace disables the module's implicit vendor directory. Even a
	// complete vendor tree can contain intentional local patches; never silently
	// replace them with upstream dependencies or attempt to synthesize a new tree.
	if info, err := os.Stat(filepath.Join(root, "vendor")); err == nil && info.IsDir() {
		return "Go vendor directory preserved; use traceai setup go and re-vendor for instrumentation"
	}
	if agent == "" {
		return "Go agent source is unavailable; use a full build or traceai setup go"
	}
	patterns, err := goTargets(args, sub)
	if err != nil {
		return err.Error()
	}
	// Respect package selection flags. An explicit command-line overlay would
	// override our GOFLAGS overlay, so keep that command untouched instead.
	var selection []string
	for i := 0; i < len(buildFlags); i++ {
		key, _, equal := strings.Cut(buildFlags[i], "=")
		if key == "-overlay" || key == "-modfile" || key == "-C" {
			return "explicit Go overlay, modfile or working-directory flag preserved; use traceai setup go"
		}
		if key == "-tags" || key == "-compiler" {
			selection = append(selection, buildFlags[i])
			if !equal && i+1 < len(buildFlags) {
				i++
				selection = append(selection, buildFlags[i])
			}
		} else if key == "-race" || key == "-msan" || key == "-asan" {
			selection = append(selection, buildFlags[i])
		} else if goFlagValues[key] && !equal {
			i++
		}
	}
	patterns = append(selection, patterns...)
	packages, err := listedGoPackages(program, working, env, patterns)
	if err != nil {
		return "could not inspect Go packages; running the original command without injection"
	}
	type overlayFile struct{ Replace map[string]string }
	overlay := overlayFile{Replace: map[string]string{}}
	for i := 0; i < len(flags); i++ {
		flag := flags[i]
		path := ""
		if strings.HasPrefix(flag, "-overlay=") {
			path = strings.TrimPrefix(flag, "-overlay=")
		} else if flag == "-overlay" && i+1 < len(flags) {
			i++
			path = flags[i]
		} else {
			continue
		}
		if !filepath.IsAbs(path) {
			path = filepath.Join(working, path)
		}
		data, e := os.ReadFile(path)
		if e != nil || json.Unmarshal(data, &overlay) != nil || overlay.Replace == nil {
			return "existing Go overlay could not be read; running without Go injection"
		}
		// Go interprets relative overlay paths relative to the command's cwd.
		normalized := map[string]string{}
		for from, to := range overlay.Replace {
			if !filepath.IsAbs(from) {
				from = filepath.Join(working, from)
			}
			if to != "" && !filepath.IsAbs(to) {
				to = filepath.Join(working, to)
			}
			normalized[from] = to
		}
		overlay.Replace = normalized
	}
	count := 0
	version := "1.26.0"
	if data, err := os.ReadFile(filepath.Join(agent, "go.mod")); err == nil {
		for _, line := range strings.Split(string(data), "\n") {
			fields := strings.Fields(line)
			if len(fields) == 2 && fields[0] == "go" && goversion.Compare("go"+fields[1], "go"+version) > 0 {
				version = fields[1]
			}
		}
	}
	for _, pkg := range packages {
		// A traced executable must not suppress independent selected targets.
		if packageHasAgent(pkg) || pkg.Name == "" || pkg.Dir == "" || sub != "test" && pkg.Name != "main" {
			continue
		}
		// Never inject into a dependency or the agent itself.
		if pkg.Module == nil || filepath.Clean(pkg.Module.Dir) != filepath.Clean(root) || pkg.Module.Path == agentModule {
			continue
		}
		if goversion.Compare("go"+pkg.Module.GoVersion, "go"+version) > 0 {
			version = pkg.Module.GoVersion
		}
		data := []byte("// Code generated by traceai. DO NOT EDIT.\npackage " + pkg.Name + "\nimport _ \"" + agentModule + "\"\n")
		source := filepath.Join(cache, "go", "sources", hashBytes(data)+".go")
		if err := cacheSubdirectory(cache, filepath.Dir(source)); err != nil {
			return "Go overlay cache is not private"
		}
		if err := atomicWrite(source, data); err != nil {
			return "Go overlay cache is not writable"
		}
		virtual := filepath.Join(pkg.Dir, "zz_traceai_auto_launcher.go")
		if _, err := os.Stat(virtual); err == nil {
			return "Go injection filename already exists; running without Go injection"
		}
		if _, exists := overlay.Replace[virtual]; exists {
			return "Go injection conflicts with an existing overlay; running without Go injection"
		}
		overlay.Replace[virtual] = source
		count++
	}
	if count == 0 {
		return ""
	}
	overlayJSON, _ := json.Marshal(overlay)
	work := []byte("go " + version + "\n\nuse (\n\t" + strconv.Quote(filepath.ToSlash(root)) + "\n\t" + strconv.Quote(filepath.ToSlash(agent)) + "\n)\n")
	identity := hashBytes(append(append([]byte{}, work...), overlayJSON...))
	generated := filepath.Join(cache, "go", "workspaces", identity)
	if err := cacheSubdirectory(cache, generated); err != nil {
		return "Go workspace cache is not private"
	}
	overlayPath := filepath.Join(generated, "overlay.json")
	if err := atomicWrite(overlayPath, overlayJSON); err != nil {
		return "Go overlay cache is not writable"
	}
	workPath := filepath.Join(generated, "go.work")
	if err := atomicWrite(workPath, work); err != nil {
		return "Go workspace cache is not writable"
	}
	flag, err := quoteGoFlag("-overlay=" + overlayPath)
	if err != nil {
		return "Go overlay path cannot be quoted; running without Go injection"
	}
	// Go applies the last occurrence of a flag. Append our merged overlay while
	// preserving the original spelling of every GOFLAGS field byte-for-byte.
	if goFlags != "" {
		flag = goFlags + " " + flag
	}
	originalModules, err := selectedGoModules(program, working, env)
	if err != nil {
		return "could not verify Go dependency selections; use traceai setup go"
	}
	injectedEnv := environmentMap(environmentList(env))
	injectedEnv["GOFLAGS"], injectedEnv["GOWORK"] = flag, workPath
	injectedModules, err := selectedGoModules(program, working, injectedEnv)
	if err != nil || !preservedGoModules(originalModules, injectedModules) {
		return "Go agent would change application dependency selections; running without Go injection; use traceai setup go"
	}
	set("GOFLAGS", flag)
	set("GOWORK", workPath)
	return ""
}
