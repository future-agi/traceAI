package launcher

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func captureCLI(t *testing.T, call func() int) (int, string, string) {
	t.Helper()
	oldOut, oldErr := os.Stdout, os.Stderr
	outR, outW, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	errR, errW, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stdout, os.Stderr = outW, errW
	defer func() { os.Stdout, os.Stderr = oldOut, oldErr }()
	outCh, errCh := make(chan []byte, 1), make(chan []byte, 1)
	go func() { data, _ := io.ReadAll(outR); outR.Close(); outCh <- data }()
	go func() { data, _ := io.ReadAll(errR); errR.Close(); errCh <- data }()
	code := call()
	outW.Close()
	errW.Close()
	return code, string(<-outCh), string(<-errCh)
}

func isolatedCLI(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	t.Chdir(dir)
	t.Setenv("TRACEAI_CACHE_DIR", filepath.Join(dir, "cache"))
	t.Setenv("FI_CONFIG_FILE", filepath.Join(dir, "config.toml"))
	put(t, filepath.Join(dir, "config.toml"), "# empty\n")
	for key, value := range map[string]string{"FI_API_KEY": "command-api-key", "FI_SECRET_KEY": "command-secret-key", "FI_BASE_URL": "http://127.0.0.1:1", "FI_PROJECT_NAME": "commands", "FI_PROJECT_TYPE": "observe", "FI_SAMPLE_RATE": "1", "FI_WIRE_MAX_BODY_BYTES": "4194304", "FI_WIRE_CAPTURE": "true", "FI_AUTO_DEBUG": "false", "OTEL_SDK_DISABLED": "false"} {
		t.Setenv(key, value)
	}
	return dir
}

func TestCommandDispatch(t *testing.T) {
	isolatedCLI(t)
	for _, tc := range []struct {
		args []string
		code int
		text string
	}{
		{nil, 2, "traceai run"}, {[]string{"help"}, 0, "setup go"},
		{[]string{"version"}, 0, Version + " (" + Flavour + ")"},
		{[]string{"version", "extra"}, 2, "takes no arguments"},
		{[]string{"unknown"}, 2, "unknown command"},
		{[]string{"run"}, 2, "requires a program"},
		{[]string{"run", "--wat"}, 2, "flag provided"},
		{[]string{"run", "--help"}, 0, "dry-run"},
		{[]string{"env", "--format", "bad"}, 2, "unsupported env format"},
		{[]string{"env", "elixir"}, 2, "unsupported runtime"},
		{[]string{"doctor", "extra"}, 2, "unexpected arguments"},
		{[]string{"validate", "extra"}, 2, "unexpected arguments"},
		{[]string{"init", "--yes", "extra"}, 2, "takes no positional"},
		{[]string{"install"}, 2, "requires a runtime"},
		{[]string{"install", "bad"}, 2, "unknown install"},
		{[]string{"install", "all", "--from", "x"}, 2, "single runtime"},
		{[]string{"install", "java", "--sha256", "x"}, 2, "requires --url"},
		{[]string{"setup", "ruby"}, 2, "setup go"},
	} {
		t.Run(strings.Join(tc.args, "_"), func(t *testing.T) {
			code, out, stderr := captureCLI(t, func() int { return Main(tc.args) })
			if code != tc.code || !strings.Contains(out+stderr, tc.text) {
				t.Fatalf("code=%d output=%q %q", code, out, stderr)
			}
		})
	}
}

func TestEnvCommandFormats(t *testing.T) {
	isolatedCLI(t)
	t.Setenv("RUBYOPT", "-w")
	for _, format := range []string{"sh", "fish", "powershell", "dockerfile", "json"} {
		t.Run(format, func(t *testing.T) {
			code, out, stderr := captureCLI(t, func() int { return Main([]string{"env", "--format", format, "ruby"}) })
			if code != 0 || stderr != "" || !strings.Contains(out, "RUBYOPT") || (format == "json") != strings.Contains(out, "***") || strings.Contains(out, "-w") || strings.Contains(out, "NODE_OPTIONS") {
				t.Fatalf("%d %s %s", code, out, stderr)
			}
			if format == "json" {
				var data map[string]string
				if err := json.Unmarshal([]byte(out), &data); err != nil {
					t.Fatal(err)
				}
			}
			if strings.Contains(out, "command-api-key") || strings.Contains(out, "command-secret-key") {
				t.Fatal("credentials leaked")
			}
		})
	}
}

func TestInitCommandNoOverwrite(t *testing.T) {
	dir := isolatedCLI(t)
	put(t, filepath.Join(dir, "Gemfile"), "source 'https://rubygems.org'")
	code, out, stderr := captureCLI(t, func() int { return Main([]string{"init", "--yes"}) })
	if code != 0 || !strings.Contains(out, "traceai install ruby") || !strings.Contains(out, "bundle exec rails") {
		t.Fatalf("%d %s %s", code, out, stderr)
	}
	path := filepath.Join(dir, "futureagi.toml")
	before, _ := os.ReadFile(path)
	if strings.Contains(string(before), "command-api-key") {
		t.Fatal("keys written into config")
	}
	code, _, _ = captureCLI(t, func() int { return Main([]string{"init", "--yes"}) })
	after, _ := os.ReadFile(path)
	if code != 1 || !bytes.Equal(before, after) {
		t.Fatal("init overwrote existing config")
	}
}

func TestValidateCommand(t *testing.T) {
	isolatedCLI(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusNoContent) }))
	defer server.Close()
	t.Setenv("FI_BASE_URL", server.URL)
	for _, args := range [][]string{{"validate", "--json"}, {"validate", "--offline", "--json"}} {
		code, out, stderr := captureCLI(t, func() int { return Main(args) })
		if code != 0 || !strings.Contains(out, `"ok": true`) || strings.Contains(out+stderr, "command-api-key") {
			t.Fatalf("%d %s %s", code, out, stderr)
		}
	}
	t.Setenv("FI_SECRET_KEY", "")
	code, _, _ := captureCLI(t, func() int { return Main([]string{"validate", "--offline"}) })
	if code != 1 {
		t.Fatal("missing key accepted")
	}
	t.Setenv("FI_SAMPLE_RATE", "NaN")
	code, out, _ := captureCLI(t, func() int { return Main([]string{"validate", "--json"}) })
	if code != 1 || !strings.Contains(out, "FI_SAMPLE_RATE") {
		t.Fatal(code, out)
	}
}

func TestDryRunCommand(t *testing.T) {
	isolatedCLI(t)
	code, out, stderr := captureCLI(t, func() int {
		return Main([]string{"run", "--dry-run", "--project", "command-api-key", "--only", "wire", "--sample-rate", "0.25", "--", "never-executed", "secret-argument"})
	})
	if code != 0 || !strings.Contains(out, `"FI_SAMPLE_RATE": "0.25"`) || !strings.Contains(out, `"FI_PROJECT_NAME": "***"`) {
		t.Fatalf("%d %s %s", code, out, stderr)
	}
	for _, secret := range []string{"command-api-key", "command-secret-key", "secret-argument"} {
		if strings.Contains(out+stderr, secret) {
			t.Fatal("dry-run leaked", secret)
		}
	}
}

func TestReleaseArchiveValidation(t *testing.T) {
	for _, tc := range []struct {
		name  string
		files map[string]string
		want  bool
	}{
		{"valid", map[string]string{"TraceAI.AutoInstrumentation.dll": "dll", "dependency.dll": "dep"}, true},
		{"traversal", map[string]string{"../escape": "bad"}, false},
		{"absolute", map[string]string{"/escape": "bad"}, false},
		{"backslash", map[string]string{`..\escape`: "bad"}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var buf bytes.Buffer
			writer := zip.NewWriter(&buf)
			for name, value := range tc.files {
				file, err := writer.Create(name)
				if err != nil {
					t.Fatal(err)
				}
				fmt.Fprint(file, value)
			}
			if err := writer.Close(); err != nil {
				t.Fatal(err)
			}
			digest := sha256.Sum256(buf.Bytes())
			_, err := releaseFiles("dotnet", buf.Bytes(), digest[:])
			if (err == nil) != tc.want {
				t.Fatalf("unexpected archive result: %v", err)
			}
			if _, err := releaseFiles("dotnet", buf.Bytes(), make([]byte, 32)); err == nil {
				t.Fatal("checksum mismatch accepted")
			}
		})
	}
	if err := installRelease("java", "http://example.com/agent.jar", strings.Repeat("a", 64), nil); err == nil {
		t.Fatal("non-HTTPS release accepted")
	}
	if err := installRelease("java", "https://example.com/agent.jar", "bad", nil); err == nil {
		t.Fatal("invalid digest accepted")
	}
}

func TestRubyInjectionSpacesAndIdempotency(t *testing.T) {
	for _, name := range []string{"cache", "cache with space", "ruby'cache", "ruby\"cache", `ruby\cache`, "cache\ttab"} {
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			env := append(baseEnv(t), "TRACEAI_CACHE_DIR="+filepath.Join(dir, name), "RUBYOPT=-w", "RUBYLIB=/user/lib")
			first := Prepare(nil, env, dir, Options{Runtimes: []string{"ruby"}})
			second := Prepare(nil, first.Environment, dir, Options{Runtimes: []string{"ruby"}})
			one, two := environmentMap(first.Environment), environmentMap(second.Environment)
			if one["RUBYOPT"] != two["RUBYOPT"] || one["RUBYLIB"] != two["RUBYLIB"] || strings.Count(one["RUBYOPT"], "-rtraceai_launcher_") != 1 {
				t.Fatal("Ruby injection not idempotent")
			}
			if strings.ContainsAny(name, " \t\r\n\v\f") && (!strings.Contains(one["RUBYLIB"], name) || strings.Contains(one["RUBYOPT"], "-I")) {
				t.Fatal("space path not using RUBYLIB")
			}
			if !strings.ContainsAny(name, " \t\r\n\v\f") && !strings.HasPrefix(one["RUBYOPT"], "-I") {
				t.Fatal("-I not prepended")
			}
		})
	}
}

func TestGoRunArgumentsNotParsedAsPackages(t *testing.T) {
	got, err := goTargets([]string{"-tags", "custom", ".", "main.go", "--output", "x@v1"}, "run")
	if err != nil || len(got) != 1 || got[0] != "." {
		t.Fatal(got, err)
	}
}

func TestWorkspaceCompatibility(t *testing.T) {
	for _, tc := range []struct {
		args []string
		want bool
	}{
		{[]string{"-mod=readonly", "."}, true},
		{[]string{"-mod=mod", "."}, false},
		{[]string{"-mod", "vendor", "."}, false},
		{[]string{"-modfile=alternate.mod", "."}, false},
		{[]string{"-o", "app", "-mod=mod", "."}, false},
		{[]string{".", "-mod=mod"}, false},
		{[]string{".", "-modfile=alternate.mod"}, false},
		{[]string{".", "-args", "-mod=mod"}, true},
	} {
		if got := workspaceCompatibleFlags(tc.args); got != tc.want {
			t.Fatalf("%v: %v", tc.args, got)
		}
	}
}

func TestDiagnosticsRejectOversizedConfiguration(t *testing.T) {
	dir := t.TempDir()
	put(t, filepath.Join(dir, "futureagi.toml"), strings.Repeat("# comment\n", 120000))
	if _, err := ResolveConfig(dir, nil); err == nil {
		t.Fatal("oversized configuration accepted")
	}
	var output discoveryBuffer
	if _, err := output.Write(make([]byte, (8<<20)+1)); err == nil || output.Len() != 0 {
		t.Fatal("unbounded package discovery")
	}
}

func TestUnsafeShimPathFailsOpen(t *testing.T) {
	dir := t.TempDir()
	env := append(baseEnv(t), "TRACEAI_CACHE_DIR="+filepath.Join(dir, "unsafe"+string(os.PathListSeparator)+"path"))
	plan := Prepare(nil, env, dir, Options{Runtimes: []string{"python", "ruby"}})
	if plan.Changes["PYTHONPATH"] != "" || plan.Changes["RUBYOPT"] != "" || len(plan.Warnings) != 2 {
		t.Fatal(plan)
	}
}

func TestOverlappingSecretMasking(t *testing.T) {
	masked := Masked(map[string]string{"FI_API_KEY": "prefix-credential", "FI_SECRET_KEY": "prefix", "FI_PROJECT_NAME": "project-prefix-credential"})
	if masked["FI_PROJECT_NAME"] != "project-***" {
		t.Fatal(masked["FI_PROJECT_NAME"])
	}
}

func TestTransitiveAgentDetection(t *testing.T) {
	if !packageHasAgent(goPackage{Deps: []string{agentModule}}) || packageHasAgent(goPackage{Imports: []string{"net/http"}}) {
		t.Fatal("transitive agent detection")
	}
}

func TestDockerfileDollarEscaping(t *testing.T) {
	dir := isolatedCLI(t)
	// Inherited option strings are opaque and masked. A generated injection-only
	// path still needs literal Dockerfile dollar escaping to remain executable.
	t.Setenv("NODE_OPTIONS", "")
	t.Setenv("TRACEAI_CACHE_DIR", filepath.Join(dir, "$application"))
	code, out, _ := captureCLI(t, func() int { return Main([]string{"env", "--format", "dockerfile", "node"}) })
	if code != 0 || !strings.Contains(out, `\$application`) || strings.Contains(out, `\\$application`) {
		t.Fatal(code, out)
	}
}
