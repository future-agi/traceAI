// Package payload stages the launcher's own guarded shims and embedded agents at
// image-build time, and copies that immutable tree into a Pod's emptyDir at init.
package payload

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
)

const Manifest = "manifest.json"
const maxBytes int64 = 256 << 20
const maxFiles = 4096

var required = []string{"node/preload.cjs", "python/sitecustomize.py", "java/traceai-javaagent.jar", "dotnet/TraceAI.AutoInstrumentation.dll", "ruby/preload.rb"}

type index struct {
	Version int               `json:"version"`
	Files   map[string]string `json:"files"`
}

// Stage is an explicit image-build operation, never run in the webhook. The full
// launcher must already embed trusted, current SDK builds. No package installation
// or network call is made here. Never inherit build-machine credentials/options.
func Stage(ctx context.Context, launcher, root string) error {
	if !filepath.IsAbs(root) || strings.ContainsAny(root, " \t\r\n\"':$") {
		return fmt.Errorf("staging root must be a simple absolute path")
	}
	if err := emptyDirectory(root); err != nil {
		return err
	}
	cmd := exec.CommandContext(ctx, launcher, "env", "--format", "json", "python", "node", "java", "dotnet", "ruby")
	cmd.Env = []string{
		"PATH=/usr/local/bin:/usr/bin:/bin",
		"GOMAXPROCS=4",
		"GOTOOLCHAIN=auto",
		"TRACEAI_CACHE_DIR=" + filepath.Join(root, "cache"),
		// Go needs a build-time import, not a Pod runtime hook. An explicitly
		// missing override suppresses the launcher's unused Go extraction.
		"TRACEAI_GO_AGENT=/__traceai_no_go_runtime_agent__",
	}
	cmd.Dir = root
	var output limitedBuffer
	cmd.Stdout = &output
	// Diagnostic output is deliberately discarded; only a fixed error is returned.
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("launcher env failed")
	}
	var env map[string]string
	if json.Unmarshal(output.data, &env) != nil {
		return fmt.Errorf("invalid launcher environment")
	}
	if err := stageEnvironment(env, root); err != nil {
		return err
	}
	return writeManifest(root)
}

type limitedBuffer struct{ data []byte }

func (b *limitedBuffer) Write(p []byte) (int, error) {
	if len(b.data)+len(p) > 1<<20 {
		return 0, fmt.Errorf("launcher output too large")
	}
	b.data = append(b.data, p...)
	return len(p), nil
}

func stageEnvironment(env map[string]string, root string) error {
	// The paths in generated shims refer to this exact cache root; copying keeps
	// those paths intact. Stable facade paths are what admission injects.
	for _, runtime := range []string{"java", "dotnet", "node", "ruby"} {
		data, err := os.ReadFile(filepath.Join(root, "cache", "agents", runtime+".json"))
		var entry string
		if err != nil || json.Unmarshal(data, &entry) != nil || !inside(root, entry) {
			return fmt.Errorf("full launcher payload missing")
		}
		if info, err := os.Lstat(entry); err != nil || !info.Mode().IsRegular() || info.Size() == 0 {
			return fmt.Errorf("full launcher payload missing")
		}
	}
	node := strings.TrimPrefix(env["NODE_OPTIONS"], "--require ")
	if len(node) >= 2 && node[0] == '"' && node[len(node)-1] == '"' {
		node = node[1 : len(node)-1]
	}
	java := strings.TrimPrefix(env["JAVA_TOOL_OPTIONS"], "-javaagent:")
	dotnet := env["DOTNET_STARTUP_HOOKS"]
	python := filepath.Join(env["PYTHONPATH"], "sitecustomize.py")
	ruby := ""
	parts := strings.Fields(env["RUBYOPT"])
	if len(parts) == 2 && strings.HasPrefix(parts[0], "-I") && strings.HasPrefix(parts[1], "-r") {
		ruby = filepath.Join(strings.TrimPrefix(parts[0], "-I"), strings.TrimPrefix(parts[1], "-r")+".rb")
	}
	for _, file := range [][2]string{{node, "node/preload.cjs"}, {java, "java/traceai-javaagent.jar"}, {python, "python/sitecustomize.py"}, {ruby, "ruby/preload.rb"}} {
		if !inside(root, file[0]) {
			return fmt.Errorf("invalid launcher payload path")
		}
		if err := copyFile(file[0], filepath.Join(root, filepath.FromSlash(file[1]))); err != nil {
			return fmt.Errorf("cannot stage launcher payload")
		}
	}
	if !inside(root, dotnet) {
		return fmt.Errorf("invalid startup hook path")
	}
	dir := filepath.Dir(dotnet)
	return filepath.WalkDir(dir, func(name string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		rel, err := filepath.Rel(dir, name)
		if err != nil {
			return err
		}
		return copyFile(name, filepath.Join(root, "dotnet", rel))
	})
}

func inside(root, name string) bool {
	if name == "" || !filepath.IsAbs(name) {
		return false
	}
	rel, err := filepath.Rel(root, name)
	return err == nil && rel != "." && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

func emptyDirectory(root string) error {
	if err := os.MkdirAll(root, 0755); err != nil {
		return fmt.Errorf("payload directory unavailable")
	}
	info, err := os.Lstat(root)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("payload directory is not a real directory")
	}
	entries, err := os.ReadDir(root)
	if err != nil || len(entries) != 0 {
		return fmt.Errorf("payload directory must be empty")
	}
	return nil
}

func fileIndex(root string) (map[string]string, error) {
	files := map[string]string{}
	remaining := maxBytes
	entries := 0
	err := filepath.WalkDir(root, func(name string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		entries++
		rel, err := filepath.Rel(root, name)
		if err != nil || entries > 2*maxFiles+1 || strings.Count(filepath.ToSlash(rel), "/") > 32 {
			return fmt.Errorf("payload tree exceeds safety limit")
		}
		if d.Type()&os.ModeSymlink != 0 {
			return fmt.Errorf("payload symlink rejected")
		}
		if d.IsDir() {
			return nil
		}
		if rel == Manifest {
			return nil
		}
		info, err := d.Info()
		if err != nil || !info.Mode().IsRegular() {
			return fmt.Errorf("payload must contain regular files only")
		}
		if len(files) >= maxFiles || info.Size() > remaining {
			return fmt.Errorf("payload exceeds safety limit")
		}
		file, err := os.Open(name)
		if err != nil {
			return err
		}
		h := sha256.New()
		n, err := io.Copy(h, io.LimitReader(file, remaining+1))
		closeErr := file.Close()
		remaining -= n
		if err != nil || closeErr != nil || remaining < 0 {
			return fmt.Errorf("cannot hash payload")
		}
		files[filepath.ToSlash(rel)] = hex.EncodeToString(h.Sum(nil))
		return nil
	})
	return files, err
}

func writeManifest(root string) error {
	files, err := fileIndex(root)
	if err != nil {
		return err
	}
	for _, name := range required {
		if _, exists := files[name]; !exists {
			return fmt.Errorf("incomplete payload")
		}
	}
	data, err := json.Marshal(index{Version: 1, Files: files})
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(root, Manifest), data, 0644)
}

// Copy validates the entire source before writing. Destination must be a real
// directory with only known payload files. Interrupted copies can safely resume;
// symlinks, unexpected files, missing source files and source tampering are rejected.
// Nonzero exit on copy failure is intentional: starting a JVM with a missing
// -javaagent would crash it. Kubernetes cannot make image-pull/init failures
// admission-fail-open; this boundary is documented in the component README.
func Copy(source, target string) error {
	info, err := os.Lstat(source)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("invalid payload source")
	}
	mf, err := os.Lstat(filepath.Join(source, Manifest))
	if err != nil || !mf.Mode().IsRegular() || mf.Size() > 1<<20 {
		return fmt.Errorf("invalid payload manifest")
	}
	data, err := os.ReadFile(filepath.Join(source, Manifest))
	if err != nil {
		return fmt.Errorf("payload manifest unavailable")
	}
	var expected index
	if json.Unmarshal(data, &expected) != nil || expected.Version != 1 || len(expected.Files) == 0 || len(expected.Files) > maxFiles {
		return fmt.Errorf("invalid payload manifest")
	}
	actual, err := fileIndex(source)
	if err != nil {
		return err
	}
	if len(actual) != len(expected.Files) {
		return fmt.Errorf("payload inventory mismatch")
	}
	for name, digest := range expected.Files {
		if actual[name] != digest {
			return fmt.Errorf("payload digest mismatch")
		}
	}
	for _, name := range required {
		if _, ok := actual[name]; !ok {
			return fmt.Errorf("incomplete payload")
		}
	}
	if err := os.MkdirAll(target, 0755); err != nil {
		return fmt.Errorf("payload target unavailable")
	}
	targetInfo, err := os.Lstat(target)
	if err != nil || !targetInfo.IsDir() || targetInfo.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("invalid payload target")
	}
	partial, err := fileIndex(target)
	if err != nil {
		return err
	}
	for name := range partial {
		if _, known := actual[name]; !known {
			return fmt.Errorf("unexpected target file")
		}
	}
	names := make([]string, 0, len(actual))
	for name := range actual {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		if partial[name] == actual[name] {
			continue
		}
		destination := filepath.Join(target, filepath.FromSlash(name))
		// Init containers are serialized. A previous crashed copy may have left a
		// short file; replace only names authenticated by the image manifest.
		if _, exists := partial[name]; exists {
			if err := os.Remove(destination); err != nil {
				return fmt.Errorf("cannot resume payload copy")
			}
		}
		if err := copyFile(filepath.Join(source, filepath.FromSlash(name)), destination); err != nil {
			return fmt.Errorf("payload copy failed")
		}
	}
	verified, err := fileIndex(target)
	if err != nil || len(verified) != len(actual) {
		return fmt.Errorf("target verification failed")
	}
	for name, digest := range actual {
		if verified[name] != digest {
			return fmt.Errorf("target verification failed")
		}
	}
	// Allow every application UID to read the payload; only init mounts it RW.
	return nil
}

func copyFile(source, target string) error {
	info, err := os.Lstat(source)
	if err != nil || !info.Mode().IsRegular() {
		return fmt.Errorf("non-regular payload file")
	}
	if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
		return err
	}
	in, err := os.Open(source)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0644)
	if err != nil {
		return err
	}
	n, err := io.Copy(out, io.LimitReader(in, maxBytes+1))
	closeErr := out.Close()
	if err != nil || closeErr != nil || n > maxBytes {
		return fmt.Errorf("payload copy failed")
	}
	return nil
}
