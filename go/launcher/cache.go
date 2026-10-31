package launcher

import (
	"bytes"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
)

//go:embed shims/python/sitecustomize.py shims/node/preload.cjs shims/ruby/preload.rb
var shims embed.FS

const agentModule = "github.com/future-agi/traceAI/go/auto"

func cacheDirectory(env map[string]string) (string, error) {
	root := env["TRACEAI_CACHE_DIR"]
	if root == "" {
		var err error
		root, err = os.UserCacheDir()
		if err != nil {
			return "", err
		}
		root = filepath.Join(root, "traceai", Version)
	}
	root, err := filepath.Abs(root)
	if err != nil {
		return "", err
	}
	if err := privateCacheDirectory(root); err != nil {
		return "", err
	}
	return root, nil
}

// Never load executable payloads from a shared or symlinked cache root. Do not
// chmod a caller-owned directory: decline injection instead of changing it.
func privateCacheDirectory(path string) error {
	if path == "" {
		return fmt.Errorf("cache path is empty")
	}
	if err := os.MkdirAll(path, 0700); err != nil {
		return err
	}
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || runtime.GOOS != "windows" && info.Mode().Perm()&0077 != 0 {
		return fmt.Errorf("cache directory must be private and not a symlink")
	}
	return nil
}

func cacheSubdirectory(cache, path string) error {
	if err := privateCacheDirectory(cache); err != nil {
		return err
	}
	relative, err := filepath.Rel(cache, path)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) || filepath.IsAbs(relative) {
		return fmt.Errorf("cache path escapes its root")
	}
	current := cache
	if relative != "." {
		for _, part := range strings.Split(relative, string(filepath.Separator)) {
			current = filepath.Join(current, part)
			if err := privateCacheDirectory(current); err != nil {
				return err
			}
		}
	}
	return nil
}

// atomicWrite never exposes a partial file. Concurrent writers produce identical
// content or replace an old pointer atomically, not an in-place truncated file.
func atomicWrite(path string, data []byte) error {
	if info, err := os.Lstat(path); err == nil && !info.Mode().IsRegular() {
		return fmt.Errorf("cache file must be regular and not a symlink")
	}
	if fileMatches(path, data) {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".traceai-*")
	if err != nil {
		return err
	}
	tmp := f.Name()
	defer os.Remove(tmp)
	if _, err = f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	if err = os.Rename(tmp, path); err != nil {
		// Windows cannot replace an existing file with Rename. Content-addressed
		// payloads need no replacement if a concurrent writer already completed it.
		if fileMatches(path, data) {
			return nil
		}
		return err
	}
	return nil
}

// Avoid reading an arbitrarily large corrupted cache file just to compare it
// with a small trusted payload. Atomic writers can only replace complete files.
func fileMatches(path string, data []byte) bool {
	f, err := os.Open(path)
	if err != nil {
		return false
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() != int64(len(data)) {
		return false
	}
	old, err := io.ReadAll(io.LimitReader(f, int64(len(data))+1))
	return err == nil && bytes.Equal(old, data)
}

func hashBytes(data []byte) string { h := sha256.Sum256(data); return hex.EncodeToString(h[:16]) }

func writeShim(cache, language, name string, data []byte) (string, error) {
	path := filepath.Join(cache, "shims", language, hashBytes(data), name)
	if err := cacheSubdirectory(cache, filepath.Dir(path)); err != nil {
		return "", err
	}
	return path, atomicWrite(path, data)
}

type Agents struct {
	Java   string `json:"java,omitempty"`
	Dotnet string `json:"dotnet,omitempty"`
	Node   string `json:"node,omitempty"`
	Go     string `json:"go,omitempty"`
	Ruby   string `json:"ruby,omitempty"`
}

func readableFile(path string) bool {
	// Reject special files before Open: opening an override FIFO without a
	// writer would otherwise hang even an unrelated application's startup.
	info, err := os.Stat(path)
	if err != nil || !info.Mode().IsRegular() || info.Size() == 0 {
		return false
	}
	f, err := os.Open(path)
	if err != nil {
		return false
	}
	defer f.Close()
	s, err := f.Stat()
	return err == nil && s.Mode().IsRegular() && s.Size() > 0
}

func agentEntry(runtime string) string {
	switch runtime {
	case "java":
		return "traceai-javaagent.jar"
	case "dotnet":
		return "TraceAI.AutoInstrumentation.dll"
	case "node":
		return "preload.cjs"
	case "go":
		return "go.mod"
	case "ruby":
		return "traceai/auto.rb"
	}
	return ""
}

func (a *Agents) set(runtime, path string) {
	switch runtime {
	case "java":
		a.Java = path
	case "dotnet":
		a.Dotnet = path
	case "node":
		a.Node = path
	case "go":
		a.Go = filepath.Dir(path)
	case "ruby":
		a.Ruby = filepath.Dir(filepath.Dir(path))
	}
}

// cachedAgent verifies the complete payload against its content-addressed root,
// not merely its entry point. Missing, changed, extra or symlinked files invalidate
// the hit; full builds then repair it from their embedded bytes, slim builds skip it.
func cachedAgent(cache, language string) string {
	pointer := filepath.Join(cache, "agents", language+".json")
	info, err := os.Lstat(pointer)
	if err != nil || !info.Mode().IsRegular() || info.Size() > 16<<10 {
		return ""
	}
	data, err := os.ReadFile(pointer)
	var path string
	if err != nil || json.Unmarshal(data, &path) != nil {
		return ""
	}
	root := path
	for range strings.Split(agentEntry(language), "/") {
		root = filepath.Dir(root)
	}
	if path != filepath.Join(root, filepath.FromSlash(agentEntry(language))) || filepath.Dir(root) != filepath.Join(cache, "agents", language) {
		return ""
	}
	if err := cacheSubdirectory(cache, root); err != nil {
		return ""
	}
	if !validPayload(root) || !readableFile(path) {
		return ""
	}
	return path
}

const maxCachedPayloadBytes = 256 << 20

func validPayload(root string) bool {
	names := []string{}
	var total int64
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		// Another atomic writer may still be preparing an identical file; it can
		// rename that temporary entry between ReadDir and Info. Do not stat it.
		if d.Type().IsRegular() && strings.HasPrefix(d.Name(), ".traceai-") {
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 || runtime.GOOS != "windows" && (info.Mode().Perm()&0022 != 0 || info.IsDir() && info.Mode().Perm()&0077 != 0) {
			return fmt.Errorf("unsafe cached payload")
		}
		if !info.IsDir() {
			if !info.Mode().IsRegular() {
				return fmt.Errorf("invalid cached payload")
			}
			total += info.Size()
			if total > maxCachedPayloadBytes || len(names) >= 4096 {
				return fmt.Errorf("cached payload exceeds verification limits")
			}
			relative, _ := filepath.Rel(root, path)
			names = append(names, filepath.ToSlash(relative))
		}
		return nil
	})
	if err != nil {
		return false
	}
	sort.Strings(names)
	h := sha256.New()
	remaining := int64(maxCachedPayloadBytes)
	for _, name := range names {
		f, err := os.Open(filepath.Join(root, filepath.FromSlash(name)))
		if err != nil {
			return false
		}
		h.Write([]byte(name))
		h.Write([]byte{0})
		n, err := io.Copy(h, io.LimitReader(f, remaining+1))
		remaining -= n
		closeErr := f.Close()
		if err != nil || closeErr != nil || remaining < 0 {
			return false
		}
		h.Write([]byte{0})
	}
	return hex.EncodeToString(h.Sum(nil)[:16]) == filepath.Base(root)
}

func discoverAgents(cache string, env map[string]string) Agents {
	var agents Agents
	if err := privateCacheDirectory(cache); err != nil {
		return agents
	}
	for _, runtime := range []string{"java", "dotnet", "node", "go", "ruby"} {
		override := env["TRACEAI_"+strings.ToUpper(runtime)+"_AGENT"]
		if override != "" {
			if s, err := os.Stat(override); err == nil && s.IsDir() {
				override = filepath.Join(override, agentEntry(runtime))
			}
			if absolute, err := filepath.Abs(override); err == nil && readableFile(absolute) {
				agents.set(runtime, absolute)
			}
			continue // An explicit missing override must not silently select another version.
		}
		if path := cachedAgent(cache, runtime); path != "" {
			agents.set(runtime, path)
			continue
		}
		if root, err := embeddedAgent(runtime, cache); err == nil && root != "" {
			agents.set(runtime, filepath.Join(root, agentEntry(runtime)))
		}
	}
	return agents
}

// installFiles places immutable, content-addressed payloads in the private cache.
// The per-runtime pointer is published last, only after every file is complete.
func installFiles(cache, runtime string, files map[string][]byte) (string, error) {
	if len(files[agentEntry(runtime)]) == 0 {
		return "", fmt.Errorf("%s agent is missing %s", runtime, agentEntry(runtime))
	}
	names := make([]string, 0, len(files))
	for name := range files {
		clean := filepath.Clean(filepath.FromSlash(name))
		if clean == "." || filepath.IsAbs(clean) || clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
			return "", fmt.Errorf("invalid agent file path")
		}
		names = append(names, name)
	}
	sort.Strings(names)
	h := sha256.New()
	for _, name := range names {
		h.Write([]byte(name))
		h.Write([]byte{0})
		h.Write(files[name])
		h.Write([]byte{0})
	}
	root := filepath.Join(cache, "agents", runtime, hex.EncodeToString(h.Sum(nil)[:16]))
	if err := cacheSubdirectory(cache, root); err != nil {
		return "", err
	}
	if err := prunePayload(root, names); err != nil {
		return "", err
	}
	for _, name := range names {
		path := filepath.Join(root, filepath.FromSlash(name))
		if err := cacheSubdirectory(cache, filepath.Dir(path)); err != nil {
			return "", err
		}
		if info, err := os.Lstat(path); err == nil && !info.Mode().IsRegular() {
			return "", fmt.Errorf("cached payload file is not regular")
		}
		if err := atomicWrite(path, files[name]); err != nil {
			return "", err
		}
	}
	if !validPayload(root) {
		return "", fmt.Errorf("cached payload failed integrity verification")
	}
	path, _ := json.Marshal(filepath.Join(root, agentEntry(runtime)))
	if err := atomicWrite(filepath.Join(cache, "agents", runtime+".json"), path); err != nil {
		return "", err
	}
	return root, nil
}

// Reinstallation must remove unexpected entries as well as restore expected
// bytes. WalkDir never follows symlinks; RemoveAll of an extra symlink removes
// the link, not its target. Identical concurrent writers keep their temp files.
func prunePayload(root string, names []string) error {
	wanted := map[string]bool{".": true}
	for _, name := range names {
		path := filepath.Clean(filepath.FromSlash(name))
		for path != "." {
			wanted[path] = true
			path = filepath.Dir(path)
		}
	}
	return filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if os.IsNotExist(err) {
			return nil // an identical writer may have renamed its temporary file
		}
		if err != nil {
			return err
		}
		relative, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		if wanted[relative] {
			if entry.IsDir() {
				return privateCacheDirectory(path)
			}
			return nil
		}
		if entry.Type().IsRegular() && strings.HasPrefix(entry.Name(), ".traceai-") {
			return nil
		}
		if err := os.RemoveAll(path); err != nil {
			return err
		}
		if entry.IsDir() {
			return filepath.SkipDir
		}
		return nil
	})
}

func filesFromFS(source fs.FS, root string) (map[string][]byte, error) {
	files := map[string][]byte{}
	err := fs.WalkDir(source, root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		// Check before opening: a FIFO may block in Open, before ReadFile can
		// inspect it. Info also handles filesystems with unknown DirEntry.Type.
		if !info.Mode().IsRegular() {
			return fmt.Errorf("agent files must be regular, not symlinks or special files")
		}
		data, err := fs.ReadFile(source, path)
		if err != nil {
			return err
		}
		name := path
		if root != "." {
			name = strings.TrimPrefix(strings.TrimPrefix(path, root), "/")
		}
		files[name] = data
		return nil
	})
	return files, err
}
