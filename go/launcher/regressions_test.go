package launcher

import (
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strconv"
	"strings"
	"testing"
)

func TestGoOptionWordsPreserveLiteralValues(t *testing.T) {
	for _, tc := range []struct {
		raw  string
		want []string
	}{
		{`-p=2 -ldflags=-X=main.root=C:\data`, []string{`-p=2`, `-ldflags=-X=main.root=C:\data`}},
		{`'-ldflags=-X=main.root=C:\my data'`, []string{`-ldflags=-X=main.root=C:\my data`}},
		{`-tags='hello world'`, []string{`-tags='hello`, `world'`}},
		{`"-tags=hello world" -p=2`, []string{`-tags=hello world`, `-p=2`}},
		{`"-tags=a"-p=2`, []string{`-tags=a`, `-p=2`}},
	} {
		t.Run(tc.raw, func(t *testing.T) {
			got, err := goOptionWords(tc.raw)
			if err != nil || !reflect.DeepEqual(got, tc.want) {
				t.Fatal(got, err)
			}
		})
	}
	for _, flag := range []string{`-overlay=C:\data\overlay.json`, "-overlay=/my path/overlay.json", "-overlay=/my 'path/overlay.json", `-overlay=/my "path/overlay.json`, "-overlay=/cache\ttab/overlay.json"} {
		quoted, err := quoteGoFlag(flag)
		got, parseErr := goOptionWords(quoted)
		if err != nil || parseErr != nil || !reflect.DeepEqual(got, []string{flag}) {
			t.Fatal(flag, quoted, got, err, parseErr)
		}
	}
	if _, err := quoteGoFlag("-overlay=/both '\" quotes/file"); err == nil {
		t.Fatal("accepted an unrepresentable Go field")
	}
	if _, err := goOptionWords(`'-tags=broken`); err == nil {
		t.Fatal("accepted an unclosed Go quote")
	}
}

func TestGoBuildFlagsRespectArgumentBoundaries(t *testing.T) {
	for _, tc := range []struct {
		sub        string
		args, want []string
	}{
		{"test", []string{".", "-mod=mod", "-tags", "custom", "-args", "-modfile=application"}, []string{"-mod=mod", "-tags", "custom"}},
		{"build", []string{".", "-modfile", "alternate.mod"}, []string{"-modfile", "alternate.mod"}},
		{"run", []string{"-tags", "custom", ".", "-mod=mod"}, []string{"-tags", "custom"}},
		{"test", []string{".", "--", "-mod=mod"}, nil},
	} {
		if got := goBuildFlags(tc.args, tc.sub); !reflect.DeepEqual(got, tc.want) {
			t.Fatalf("%s %v: %v", tc.sub, tc.args, got)
		}
	}
}

func TestGoFlagTerminatorTargetSelection(t *testing.T) {
	for _, tc := range []struct {
		sub        string
		args, want []string
	}{
		{"run", []string{"-tags", "custom", "--", "./cmd/app", "--argument"}, []string{"./cmd/app"}},
		{"build", []string{"--", "./cmd/a", "./cmd/b"}, []string{"./cmd/a", "./cmd/b"}},
		{"install", []string{"--", "./cmd/app"}, []string{"./cmd/app"}},
		{"test", []string{"./pkg", "--", "./application-argument"}, []string{"./pkg"}},
		{"test", []string{"--", "./application-argument"}, []string{"."}},
	} {
		t.Run(tc.sub+strings.Join(tc.args, " "), func(t *testing.T) {
			got, err := goTargets(tc.args, tc.sub)
			if err != nil || !reflect.DeepEqual(got, tc.want) {
				t.Fatal(got, err)
			}
			flags := goBuildFlags(tc.args, tc.sub)
			if tc.sub == "run" {
				if !reflect.DeepEqual(flags, []string{"-tags", "custom"}) {
					t.Fatal(flags)
				}
			} else if len(flags) != 0 {
				t.Fatal("read flags after terminator", flags)
			}
		})
	}
}

func TestTestOnlyImportsDoNotImplyProductionAgent(t *testing.T) {
	for _, pkg := range []goPackage{{TestImports: []string{agentModule}}, {XTestImports: []string{agentModule}}} {
		if packageHasAgent(pkg) {
			t.Fatal("test-only import suppressed production instrumentation")
		}
	}
}

func TestCacheIntegrityRejectsIncompleteAndChangedPayloads(t *testing.T) {
	for _, change := range []string{"entry", "dependency", "missing", "extra", "pointer", "symlink", "oversized"} {
		t.Run(change, func(t *testing.T) {
			cache := filepath.Join(t.TempDir(), "cache")
			root, err := installFiles(cache, "node", map[string][]byte{"preload.cjs": []byte("ORIGINAL"), "hook.mjs": []byte("DEPENDENCY")})
			if err != nil || cachedAgent(cache, "node") == "" {
				t.Fatal("fresh payload rejected", err)
			}
			switch change {
			case "entry":
				put(t, filepath.Join(root, "preload.cjs"), "TAMPERED_FULL")
			case "dependency":
				put(t, filepath.Join(root, "hook.mjs"), "TAMPERED_DEPENDENCY")
			case "missing":
				if err := os.Remove(filepath.Join(root, "hook.mjs")); err != nil {
					t.Fatal(err)
				}
			case "extra":
				put(t, filepath.Join(root, "extra.cjs"), "EXTRA")
			case "pointer":
				put(t, filepath.Join(cache, "agents/node.json"), strconv.Quote(filepath.Join(t.TempDir(), "preload.cjs")))
			case "oversized":
				if err := os.Truncate(filepath.Join(root, "hook.mjs"), maxCachedPayloadBytes+1); err != nil {
					t.Fatal(err)
				}
			case "symlink":
				if runtime.GOOS == "windows" {
					t.Skip("requires symlink privilege")
				}
				if err := os.Remove(filepath.Join(root, "hook.mjs")); err != nil {
					t.Fatal(err)
				}
				other := filepath.Join(t.TempDir(), "hook.mjs")
				put(t, other, "DEPENDENCY")
				if err := os.Symlink(other, filepath.Join(root, "hook.mjs")); err != nil {
					t.Fatal(err)
				}
			}
			if got := cachedAgent(cache, "node"); got != "" {
				t.Fatal("accepted corrupted cache", got)
			}
		})
	}
}

func TestSharedCacheRootFailsOpen(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Unix mode bits; Windows ACLs require runtime verification")
	}
	cache := filepath.Join(t.TempDir(), "shared")
	if err := os.Mkdir(cache, 0777); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(cache, 0777); err != nil {
		t.Fatal(err)
	}
	original := []string{"TRACEAI_CACHE_DIR=" + cache, "NODE_OPTIONS=--title=application", "KEEP=unchanged"}
	plan := Prepare(nil, original, t.TempDir(), Options{})
	env := environmentMap(plan.Environment)
	if len(plan.Warnings) == 0 || env["NODE_OPTIONS"] != "--title=application" || env["KEEP"] != "unchanged" || env["PYTHONPATH"] != "" {
		t.Fatal(plan)
	}
	info, err := os.Stat(cache)
	if err != nil || info.Mode().Perm() != 0777 {
		t.Fatal("changed caller-owned directory", err)
	}
}

func TestConfigMasksCompleteInheritedCredentials(t *testing.T) {
	isolatedCLI(t)
	secret := "VERIFY_FAKE_SECRET_93817"
	t.Setenv("AWS_SECRET_ACCESS_KEY", secret)
	t.Setenv("FI_PROJECT_NAME", secret)
	for _, args := range [][]string{{"run", "--dry-run", "--", "true"}, {"validate", "--offline", "--json"}} {
		code, out, stderr := captureCLI(t, func() int { return Main(args) })
		if code != 0 || strings.Contains(out+stderr, secret) || !strings.Contains(out, `"FI_PROJECT_NAME": "***"`) {
			t.Fatalf("%v: %d %s %s", args, code, out, stderr)
		}
	}
	// An override must not erase a credential from the redaction context.
	got := displayed(map[string]string{"FI_API_KEY": "replacement", "FI_PROJECT_NAME": "command-api-key"}, os.Environ())
	if got["FI_PROJECT_NAME"] != "***" {
		t.Fatal(got)
	}
}

func TestGoFlagsDiagnosticsNeverExposeOpaqueApplicationFlags(t *testing.T) {
	// Unlike inherited credential variables, a GOENV-only linker secret has no
	// named environment entry from which diagnostics can learn its value.
	raw := `-p=2 -ldflags=-X=main.token=PERSISTED_LINKER_SECRET_93817 -overlay=/cache/overlay.json`
	values := map[string]string{"GOFLAGS": raw, "GOWORK": "/cache/go.work"}
	got := Masked(values)
	if got["GOFLAGS"] != "***" || got["GOWORK"] != values["GOWORK"] {
		t.Fatal("opaque flags appeared in diagnostics", got)
	}
	if values["GOFLAGS"] != raw {
		t.Fatal("display masking changed the application's actual build flags")
	}
}

func TestDiagnosticErrorsMaskCredentials(t *testing.T) {
	dir := isolatedCLI(t)
	secret := "VERIFY_FAKE_API_93817"
	t.Setenv("FI_API_KEY", secret)
	t.Setenv("FI_SECRET_KEY", "VERIFY_FAKE_KEY_23914")
	for _, tc := range []struct {
		args []string
		code int
	}{
		{[]string{"run", "--", secret}, 127},
		{[]string{"run", "--debug=VERIFY_FAKE_KEY_23914", "--", "true"}, 2},
		{[]string{"env", "--VERIFY_FAKE_API_93817"}, 2},
		{[]string{"install", "java", "--VERIFY_FAKE_API_93817"}, 2},
		{[]string{"validate", "--VERIFY_FAKE_API_93817"}, 2},
	} {
		code, out, stderr := captureCLI(t, func() int { return Main(tc.args) })
		if code != tc.code || strings.Contains(out+stderr, secret) || strings.Contains(out+stderr, "VERIFY_FAKE_KEY_23914") {
			t.Fatalf("%v: %d %s %s", tc.args, code, out, stderr)
		}
	}
	put(t, filepath.Join(dir, secret), "not executable")
	code, _, stderr := captureCLI(t, func() int { return runCLI([]string{"--", "./" + secret}, dir) })
	if code != 126 || strings.Contains(stderr, secret) {
		t.Fatal(code, stderr)
	}
	t.Setenv("FI_SECRET_KEY", "quoted\"credential\tvalue")
	_, _, stderr = captureCLI(t, func() int { return Main([]string{"run", "--debug=quoted\"credential\tvalue", "--", "true"}) })
	if strings.Contains(stderr, "credential") {
		t.Fatal("escaped flag diagnostic leaked", stderr)
	}
}

func TestNodeOptionsLiteralPathEncoding(t *testing.T) {
	for _, name := range []string{"cache\ttab", "cache'quote", "cache\"quote", `cache\slash`, "cache\u0085unicode"} {
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			env := append(baseEnv(t), "TRACEAI_CACHE_DIR="+filepath.Join(dir, name), "NODE_OPTIONS=--title=application")
			first := Prepare(nil, env, dir, Options{Runtimes: []string{"node"}})
			second := Prepare(nil, first.Environment, dir, Options{Runtimes: []string{"node"}})
			if !reflect.DeepEqual(first.Environment, second.Environment) {
				t.Fatal("Node injection not idempotent")
			}
			words, err := nodeOptionWords(environmentMap(first.Environment)["NODE_OPTIONS"])
			if err != nil || len(words) != 3 || words[0] != "--require" || !readableFile(words[1]) || !strings.Contains(words[1], name) {
				t.Fatal(words, err)
			}
		})
	}
}
