package payload

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func put(t *testing.T, root, name, value string) string {
	t.Helper()
	p := filepath.Join(root, filepath.FromSlash(name))
	if err := os.MkdirAll(filepath.Dir(p), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(value), 0644); err != nil {
		t.Fatal(err)
	}
	return p
}
func sourceFixture(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	for _, name := range required {
		put(t, root, name, "trusted fixture "+name)
	}
	put(t, root, "dotnet/dependency.dll", "dependency")
	if err := writeManifest(root); err != nil {
		t.Fatal(err)
	}
	return root
}

func TestCopyPayloadReadOnlyConsumerAndRestart(t *testing.T) {
	source := sourceFixture(t)
	target := filepath.Join(t.TempDir(), "shared")
	if err := Copy(source, target); err != nil {
		t.Fatal(err)
	}
	files, err := fileIndex(source)
	if err != nil {
		t.Fatal(err)
	}
	for name := range files {
		p := filepath.Join(target, filepath.FromSlash(name))
		info, err := os.Stat(p)
		if err != nil || info.Mode().Perm() != 0644 {
			t.Fatalf("not readable by application UIDs: %s", name)
		}
	}
	if err := Copy(source, target); err != nil {
		t.Fatalf("init retry is not idempotent: %v", err)
	}
	if err := os.WriteFile(filepath.Join(target, "node", "preload.cjs"), []byte("interrupted copy"), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(target, "ruby", "preload.rb")); err != nil {
		t.Fatal(err)
	}
	if err := Copy(source, target); err != nil {
		t.Fatalf("cannot repair interrupted init: %v", err)
	}
	after, err := fileIndex(target)
	if err != nil {
		t.Fatal(err)
	}
	for name, digest := range files {
		if after[name] != digest {
			t.Fatal("retry left corrupt payload")
		}
	}
}

func TestCopyRejectsTamperingBeforeAnyWrite(t *testing.T) {
	for _, tc := range []struct {
		name   string
		change func(*testing.T, string)
	}{
		{"modified", func(t *testing.T, s string) { put(t, s, "java/traceai-javaagent.jar", "tampered") }},
		{"extra-file", func(t *testing.T, s string) { put(t, s, "extra-executable", "not authorized") }},
		{"missing-file", func(t *testing.T, s string) {
			if err := os.Remove(filepath.Join(s, "node", "preload.cjs")); err != nil {
				t.Fatal(err)
			}
		}},
		{"file-symlink", func(t *testing.T, s string) {
			if err := os.Symlink("java/traceai-javaagent.jar", filepath.Join(s, "link")); err != nil {
				t.Fatal(err)
			}
		}},
		{"directory-symlink", func(t *testing.T, s string) {
			if err := os.Symlink(filepath.Join(s, "java"), filepath.Join(s, "link")); err != nil {
				t.Fatal(err)
			}
		}},
		{"manifest-traversal", func(t *testing.T, s string) { put(t, s, Manifest, `{"version":1,"files":{"../escape":"digest"}}`) }},
		{"manifest-version", func(t *testing.T, s string) { put(t, s, Manifest, `{"version":99,"files":{}}`) }},
		{"manifest-symlink", func(t *testing.T, s string) {
			if err := os.Remove(filepath.Join(s, Manifest)); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink("node/preload.cjs", filepath.Join(s, Manifest)); err != nil {
				t.Fatal(err)
			}
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			source := sourceFixture(t)
			tc.change(t, source)
			target := filepath.Join(t.TempDir(), "untouched")
			if err := Copy(source, target); err == nil {
				t.Fatal("unsafe source accepted")
			}
			if _, err := os.Stat(target); !os.IsNotExist(err) {
				t.Fatal("wrote destination before validation")
			}
		})
	}
}

func TestCopyRejectsUnsafeDestinations(t *testing.T) {
	source := sourceFixture(t)
	for _, tc := range []struct {
		name    string
		prepare func(*testing.T, string)
	}{
		{"unexpected-file", func(t *testing.T, s string) { put(t, s, "unknown", "owned by someone else") }},
		{"nested-symlink", func(t *testing.T, s string) {
			if err := os.Symlink(t.TempDir(), filepath.Join(s, "node")); err != nil {
				t.Fatal(err)
			}
		}},
		{"file-as-directory", func(t *testing.T, s string) { put(t, s, "node", "not a directory") }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			target := t.TempDir()
			tc.prepare(t, target)
			if err := Copy(source, target); err == nil {
				t.Fatal("unsafe destination accepted")
			}
		})
	}
	parent := t.TempDir()
	target := filepath.Join(parent, "link")
	if err := os.Symlink(t.TempDir(), target); err != nil {
		t.Fatal(err)
	}
	if err := Copy(source, target); err == nil {
		t.Fatal("destination root symlink accepted")
	}
	link := filepath.Join(t.TempDir(), "source")
	if err := os.Symlink(source, link); err != nil {
		t.Fatal(err)
	}
	if err := Copy(link, t.TempDir()); err == nil {
		t.Fatal("source root symlink accepted")
	}
}

func TestPayloadSafetyLimits(t *testing.T) {
	t.Run("oversize-file", func(t *testing.T) {
		root := t.TempDir()
		name := put(t, root, "oversize", "")
		if err := os.Truncate(name, maxBytes+1); err != nil {
			t.Fatal(err)
		}
		if _, err := fileIndex(root); err == nil {
			t.Fatal("oversize payload accepted")
		}
	})
	t.Run("deep-tree", func(t *testing.T) {
		root := t.TempDir()
		put(t, root, strings.Repeat("level/", 34)+"file", "bounded")
		if _, err := fileIndex(root); err == nil {
			t.Fatal("deep payload accepted")
		}
	})
}

func TestStageUsesLauncherEnvironmentAndCompleteDependencies(t *testing.T) {
	root := t.TempDir()
	entries := map[string]string{"java": "traceai-javaagent.jar", "dotnet": "TraceAI.AutoInstrumentation.dll", "node": "preload.cjs", "ruby": "traceai/auto.rb"}
	for language, name := range entries {
		entry := put(t, root, "cache/agents/"+language+"/hash/"+name, "embedded "+language)
		data, _ := json.Marshal(entry)
		put(t, root, "cache/agents/"+language+".json", string(data))
	}
	put(t, root, "cache/agents/dotnet/hash/dependency.dll", "complete dependency")
	node := put(t, root, "cache/shims/node/hash/preload.cjs", "guarded Node shim with fallback")
	put(t, root, "cache/shims/python/hash/sitecustomize.py", "guarded Python shim and app sitecustomize chain")
	put(t, root, "cache/shims/ruby/hash/traceai_launcher_hash.rb", "guarded Ruby shim with fallback")
	env := map[string]string{"NODE_OPTIONS": `--require "` + node + `"`, "JAVA_TOOL_OPTIONS": "-javaagent:" + filepath.Join(root, "cache/agents/java/hash/traceai-javaagent.jar"), "DOTNET_STARTUP_HOOKS": filepath.Join(root, "cache/agents/dotnet/hash/TraceAI.AutoInstrumentation.dll"), "PYTHONPATH": filepath.Join(root, "cache/shims/python/hash"), "RUBYOPT": "-I" + filepath.Join(root, "cache/shims/ruby/hash") + " -rtraceai_launcher_hash"}
	if err := stageEnvironment(env, root); err != nil {
		t.Fatal(err)
	}
	if err := writeManifest(root); err != nil {
		t.Fatal(err)
	}
	for _, name := range append(required, "dotnet/dependency.dll") {
		data, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(name)))
		if err != nil || len(data) == 0 {
			t.Fatalf("payload missing %s", name)
		}
	}
	target := filepath.Join(t.TempDir(), "copy")
	if err := Copy(root, target); err != nil {
		t.Fatal(err)
	}
}

func TestFullLauncherStageAndCopy(t *testing.T) {
	launcher := os.Getenv("TRACEAI_TEST_FULL_LAUNCHER")
	if launcher == "" {
		t.Skip("set TRACEAI_TEST_FULL_LAUNCHER to a trusted, prebuilt full launcher")
	}
	root := filepath.Join(t.TempDir(), "staged")
	if err := Stage(context.Background(), launcher, root); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(root, "cache", "agents", "go")); !os.IsNotExist(err) {
		t.Fatal("unused Go build payload was extracted")
	}
	if err := Copy(root, filepath.Join(t.TempDir(), "shared")); err != nil {
		t.Fatal(err)
	}
}

func TestStageRejectsMissingPayloadAndUnsafePaths(t *testing.T) {
	if err := stageEnvironment(map[string]string{}, t.TempDir()); err == nil {
		t.Fatal("slim launcher accepted")
	}
	for _, root := range []string{"relative", "/path with spaces", "/path:with:separator", "/path$(ENV)"} {
		if err := Stage(context.Background(), "not-executed", root); err == nil {
			t.Fatal("unsafe staging path accepted")
		}
	}
	root := t.TempDir()
	put(t, root, "existing", "do not overwrite")
	if err := Stage(context.Background(), "not-executed", root); err == nil {
		t.Fatal("nonempty staging root accepted")
	}
	if err := writeManifest(t.TempDir()); err == nil {
		t.Fatal("incomplete payload accepted")
	}
	var b limitedBuffer
	if _, err := b.Write([]byte(strings.Repeat("a", (1<<20)+1))); err == nil {
		t.Fatal("unbounded launcher output")
	}
}
