//go:build !windows

package launcher

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestInitAdviceRedactsCredentialBearingInterpreterPaths(t *testing.T) {
	python := requireProgram(t, "python3")
	for _, tc := range []struct {
		name, key, secret string
		venv              bool
	}{
		{"fi_api_key", "FI_API_KEY", "VERIFY_INIT_PROVIDER_SECRET_492018", false},
		{"fi_secret_key", "FI_SECRET_KEY", "VERIFY_INIT_FI_SECRET_492018", false},
		{"provider_key", "OPENAI_API_KEY", "VERIFY_INIT_PROVIDER_KEY_492018", false},
		{"authorization_header", "HTTP_AUTHORIZATION", "Bearer VERIFY_HEADER_CREDENTIAL_9032", false},
		{"quoted_key", "FI_API_KEY", "VERIFY_INIT_'quoted'_SECRET_492018", false},
		{"virtualenv", "FI_API_KEY", "VERIFY_INIT_VENV_SECRET_492018", true},
		{"ordinary_path", "", "", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := isolatedCLI(t)
			put(t, filepath.Join(dir, "pyproject.toml"), "# Python project\n")
			link := filepath.Join(dir, "python 'with space' "+tc.secret, "bin", "python")
			if err := os.MkdirAll(filepath.Dir(link), 0700); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(python, link); err != nil {
				t.Fatal(err)
			}
			if tc.key != "" {
				t.Setenv(tc.key, tc.secret)
			}
			t.Setenv("TRACEAI_PYTHON", link)
			if tc.venv {
				t.Setenv("TRACEAI_PYTHON", "")
				t.Setenv("VIRTUAL_ENV", filepath.Dir(filepath.Dir(link)))
			}
			code, output, stderr := captureCLI(t, func() int { return Main([]string{"init", "--yes"}) })
			want := strings.ReplaceAll(link, tc.secret, "***")
			if tc.secret == "" {
				want = link
			}
			if code != 0 || stderr != "" || !strings.Contains(output, "Run: traceai run -- "+shellQuote(want)+" app.py") {
				t.Fatal("init failed or omitted the safely quoted interpreter advice")
			}
			if tc.secret != "" && (strings.Contains(output+stderr, tc.secret) || strings.Contains(output+stderr, shellQuote(tc.secret))) {
				t.Fatal("init advice disclosed a credential")
			}
			if actual := projectPython(dir, environmentMap(os.Environ())); actual != link {
				t.Fatal("display masking changed interpreter selection")
			}
		})
	}
}

func TestInitPromptRedactsCredentialBearingProjectName(t *testing.T) {
	dir := isolatedCLI(t)
	const secret = "VERIFY_INIT_PROJECT_SECRET_492018"
	project := filepath.Join(dir, "project-"+secret)
	if err := os.Mkdir(project, 0700); err != nil {
		t.Fatal(err)
	}
	t.Chdir(project)
	t.Setenv("FI_API_KEY", secret)
	// A character-device stdin enters the prompt path without an interactive
	// test or application process. EOF accepts the unchanged default name.
	input, err := os.Open(os.DevNull)
	if err != nil {
		t.Fatal(err)
	}
	defer input.Close()
	original := os.Stdin
	os.Stdin = input
	defer func() { os.Stdin = original }()
	code, output, stderr := captureCLI(t, func() int { return Main([]string{"init"}) })
	if code != 0 || stderr != "" || !strings.Contains(output, "Project name [project-***]: ") || strings.Contains(output+stderr, secret) {
		t.Fatal("init prompt disclosed a credential or changed initialization")
	}
}
