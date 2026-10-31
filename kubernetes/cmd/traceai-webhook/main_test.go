package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestCopyPayloadCommand(t *testing.T) {
	source, target := t.TempDir(), filepath.Join(t.TempDir(), "shared")
	files := map[string]string{}
	for _, name := range []string{"node/preload.cjs", "python/sitecustomize.py", "java/traceai-javaagent.jar", "dotnet/TraceAI.AutoInstrumentation.dll", "ruby/preload.rb"} {
		path := filepath.Join(source, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
			t.Fatal(err)
		}
		data := []byte("test fixture " + name)
		if err := os.WriteFile(path, data, 0644); err != nil {
			t.Fatal(err)
		}
		digest := sha256.Sum256(data)
		files[name] = hex.EncodeToString(digest[:])
	}
	manifest, err := json.Marshal(map[string]any{"version": 1, "files": files})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(source, "manifest.json"), manifest, 0644); err != nil {
		t.Fatal(err)
	}
	if err := run([]string{"copy-payload", "--source=" + source, "--target=" + target}); err != nil {
		t.Fatal(err)
	}
	for name := range files {
		if _, err := os.Stat(filepath.Join(target, filepath.FromSlash(name))); err != nil {
			t.Fatal(err)
		}
	}
}

func TestCommandErrorsDoNotExposePathsOrSettings(t *testing.T) {
	for _, args := range [][]string{
		{"copy-payload", "--source=/missing-artifact-sensitive-path"},
		{"stage-payload", "--root=relative-sensitive-path"},
		{"--agent-image=example/image:version", "--tls-cert=/missing-sensitive-certificate", "--tls-key=/missing-sensitive-key"},
	} {
		err := run(args)
		if err == nil || strings.Contains(err.Error(), "sensitive") {
			t.Fatalf("unsafe or missing diagnostic: %v", err)
		}
	}
	t.Setenv("TRACEAI_SETTINGS_JSON", `{"FI_SECRET_KEY":"sensitive-setting"}`)
	err := run([]string{"--agent-image=example/image:version"})
	if err == nil || strings.Contains(err.Error(), "sensitive-setting") {
		t.Fatalf("unsafe settings diagnostic: %v", err)
	}
}

func TestServerRejectsMissingImageAndInvalidSettings(t *testing.T) {
	if err := run(nil); err == nil {
		t.Fatal("server must require an image")
	}
	t.Setenv("TRACEAI_SETTINGS_JSON", `{"FI_SAMPLE_RATE":true}`)
	if err := run([]string{"--agent-image=example/image:version"}); err == nil {
		t.Fatal("non-string settings accepted")
	}
}
