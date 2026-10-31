//go:build embedagents

package launcher

import (
	"bytes"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestFullCacheRepairsMissingAndTamperedFiles(t *testing.T) {
	cache := filepath.Join(t.TempDir(), "cache")
	original := discoverAgents(cache, map[string]string{})
	if original.Go == "" || original.Node == "" {
		t.Fatal("missing full payloads")
	}
	auto := filepath.Join(original.Go, "auto.go")
	goBytes, err := os.ReadFile(auto)
	if err != nil {
		t.Fatal(err)
	}
	nodeBytes, err := os.ReadFile(original.Node)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(auto); err != nil {
		t.Fatal(err)
	}
	put(t, original.Node, "console.log('TAMPERED_FULL');")
	put(t, filepath.Join(filepath.Dir(original.Node), "junk.txt"), "unexpected")
	put(t, filepath.Join(original.Go, "junk/extra.go"), "unexpected")
	repaired := discoverAgents(cache, map[string]string{})
	if repaired.Go != original.Go || repaired.Node != original.Node {
		t.Fatal("full cache repair failed", repaired)
	}
	for path, want := range map[string][]byte{auto: goBytes, original.Node: nodeBytes} {
		got, err := os.ReadFile(path)
		if err != nil || !bytes.Equal(got, want) {
			t.Fatal("payload not restored", path, err)
		}
	}
}

// Wire parsing, stream completion and export-time privacy belong to the SDKs,
// not to this launcher. Never silently ship an old SDK after its contract fixes.
func TestFullStagingMatchesCurrentAgents(t *testing.T) {
	if os.Getenv("TRACEAI_E2E") != "1" {
		t.Skip("checkout freshness checks require TRACEAI_E2E=1")
	}
	root, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	for _, language := range []string{"java", "dotnet", "node", "go", "ruby"} {
		t.Run(language, func(t *testing.T) {
			expected := map[string][]byte{}
			add := func(name, path string) {
				data, err := os.ReadFile(path)
				if err != nil {
					t.Fatal(err)
				}
				expected[filepath.ToSlash(name)] = data
			}
			var source string
			switch language {
			case "java":
				add(agentEntry(language), filepath.Join(root, "java/traceai-javaagent/target/traceai-javaagent.jar"))
			case "dotnet":
				source = filepath.Join(root, "csharp/src/TraceAI.AutoInstrumentation/bin/Release/net8.0")
			case "node":
				source = filepath.Join(root, "typescript/packages/traceai_auto/dist/agent")
			case "go":
				source = filepath.Join(root, "go/auto")
			case "ruby":
				source = filepath.Join(root, "ruby/lib")
			}
			if source != "" {
				err := filepath.WalkDir(source, func(path string, entry fs.DirEntry, err error) error {
					if err != nil {
						return err
					}
					relative, err := filepath.Rel(source, path)
					if err != nil {
						return err
					}
					if language == "dotnet" && entry.IsDir() && relative != "." {
						return filepath.SkipDir
					}
					if language == "go" && entry.IsDir() && relative != "." && (strings.HasPrefix(entry.Name(), ".") || entry.Name() == "cmd" || entry.Name() == "testdata" || entry.Name() == "vendor" || entry.Name() == "node_modules" || readableFile(filepath.Join(path, "go.mod"))) {
						return filepath.SkipDir
					}
					if entry.IsDir() {
						return nil
					}
					name := entry.Name()
					if language == "go" && entry.Type()&os.ModeSymlink != 0 {
						return nil
					}
					if language == "go" && (strings.HasSuffix(name, "_test.go") || !(strings.HasSuffix(name, ".go") || name == "go.mod" || name == "go.sum" || name == "wire-protocols.json")) {
						return nil
					}
					if language == "go" {
						relative += ".source"
					}
					add(relative, path)
					return nil
				})
				if err != nil {
					t.Fatal(err)
				}
			}
			actual, err := filesFromFS(payloads, "embedded/"+language)
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(expected, actual) {
				t.Fatal("staged SDK differs from current prebuilt artifacts/sources; run make full after SDK builds")
			}
		})
	}
}

func TestFullPayloadsExtract(t *testing.T) {
	cache := filepath.Join(t.TempDir(), "cache")
	for _, runtime := range []string{"java", "dotnet", "node", "go"} {
		t.Run(runtime, func(t *testing.T) {
			root, err := embeddedAgent(runtime, cache)
			if err != nil || !readableFile(filepath.Join(root, agentEntry(runtime))) {
				t.Fatalf("full build is missing %s payload: %v", runtime, err)
			}
			if runtime == "go" {
				if !readableFile(filepath.Join(root, "auto.go")) {
					t.Fatal("Go source filenames not restored")
				}
				if _, err := os.Stat(filepath.Join(root, "go.mod.source")); !os.IsNotExist(err) {
					t.Fatal("staging name leaked into extracted module")
				}
			}
		})
	}
	if _, present := embeddedVersions()["ruby"]; present {
		root, err := embeddedAgent("ruby", cache)
		if err != nil || !readableFile(filepath.Join(root, agentEntry("ruby"))) {
			t.Fatal("Ruby metadata and payload disagree", err)
		}
	}
}
