//go:build !windows

package launcher

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestNodeCredentialPathsNeverReachGeneratedOutput(t *testing.T) {
	for _, tc := range []struct{ name, secret string }{
		{"plain", "VERIFY_NODE_PLAIN_SECRET_9032"},
		{"quoted", `VERIFY_NODE_"QUOTED"_SECRET_9032`},
		{"backslash", `VERIFY_NODE_\BACKSLASH_SECRET_9032`},
		{"mixed", "VERIFY_NODE_\t\"\\_SECRET_9032"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := isolatedCLI(t)
			t.Setenv("FI_API_KEY", tc.secret)
			t.Setenv("TRACEAI_CACHE_DIR", filepath.Join(dir, "cache-"+tc.secret))
			t.Setenv("NODE_OPTIONS", "")
			if err := os.Unsetenv("NODE_OPTIONS"); err != nil {
				t.Fatal(err)
			}
			for _, command := range []string{"dry_run", "json", "sh", "fish", "powershell", "dockerfile"} {
				t.Run(command, func(t *testing.T) {
					args := []string{"env", "--format", command, "node"}
					if command == "dry_run" {
						args = []string{"run", "--dry-run", "--", "node", "never-executed.cjs"}
					}
					code, output, stderr := captureCLI(t, func() int { return Main(args) })
					if code != 0 || strings.Contains(output+stderr, "SECRET_9032") {
						t.Fatal("generated output disclosed a credential or failed")
					}
					if command == "dry_run" || command == "json" {
						var values map[string]string
						if command == "json" {
							if err := json.Unmarshal([]byte(output), &values); err != nil {
								t.Fatal(err)
							}
						} else {
							var plan struct {
								Environment map[string]string `json:"environment"`
							}
							if err := json.Unmarshal([]byte(output), &plan); err != nil {
								t.Fatal(err)
							}
							values = plan.Environment
						}
						words, err := nodeOptionWords(values["NODE_OPTIONS"])
						if err != nil || !strings.Contains(values["NODE_OPTIONS"], "***") {
							t.Fatal("diagnostic did not mask injection")
						}
						for _, word := range words {
							if strings.Contains(word, tc.secret) {
								t.Fatal("Node decoding recovered a credential from diagnostics")
							}
						}
					} else {
						if strings.Contains(output, "NODE_OPTIONS") || !strings.Contains(stderr, "NODE_OPTIONS injection cannot be displayed safely") {
							t.Fatal("executable output retained a credential-bearing injection literal")
						}
						if command == "sh" {
							env := environmentMap(os.Environ())
							env["NODE_OPTIONS"] = "--title=APP_RUNTIME_TITLE"
							code, actual, errout := startupProbe(t, "/bin/sh", dir, environmentList(env), "-eu", "-c", output+"printf '%s' \"$NODE_OPTIONS\"")
							if code != 0 || actual != env["NODE_OPTIONS"] || errout != "" {
								t.Fatal("omitted injection changed the later application option")
							}
						}
					}
				})
			}
			// Masking is display-only: run still passes the original Node path.
			plan := Prepare(nil, os.Environ(), dir, Options{Runtimes: []string{"node"}})
			words, err := nodeOptionWords(environmentMap(plan.Environment)["NODE_OPTIONS"])
			if err != nil || len(words) != 2 || !strings.Contains(words[1], tc.secret) || !readableFile(words[1]) {
				t.Fatal("display privacy changed executable Node injection")
			}
		})
	}
}

func TestAuthorizationCredentialsNeverReachCLIOutput(t *testing.T) {
	isolatedCLI(t)
	const header = "Bearer VERIFY_HEADER_CREDENTIAL_9032"
	t.Setenv("HTTP_AUTHORIZATION", header)
	t.Setenv("FI_PROJECT_NAME", header)
	for _, args := range [][]string{
		{"run", "--dry-run", "--", "true"},
		{"doctor", "--offline", "--json"},
		{"validate", "--offline", "--json"},
	} {
		t.Run(args[0], func(t *testing.T) {
			code, output, stderr := captureCLI(t, func() int { return Main(args) })
			if code != 0 || strings.Contains(output+stderr, "VERIFY_HEADER_CREDENTIAL_9032") || !strings.Contains(output, `"FI_PROJECT_NAME": "***"`) {
				t.Fatal("authorization-header credential reached CLI output")
			}
		})
	}
}

// Called by TestE2E with the actual full CLI built for the runtime matrix.
func checkJavaCredentialPaths(t *testing.T, binary string) {
	java := requireProgram(t, "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home/bin/java")
	root := repoRoot(t)
	for _, mode := range []string{"embedded", "override"} {
		for _, disabled := range []string{"false", "true"} {
			t.Run(mode+"/disabled_"+disabled, func(t *testing.T) {
				const secret = "VERIFY_JAVA_AGENT_PATH_SECRET_9032"
				dir := t.TempDir()
				m := newMock(t)
				env := environmentMap(testEnv(t, m, root))
				env["FI_API_KEY"], env["OTEL_SDK_DISABLED"] = secret, disabled
				env["JAVA_TOOL_OPTIONS"], env["JDK_JAVA_OPTIONS"], env["_JAVA_OPTIONS"] = "", "", ""
				env["TRACEAI_CACHE_DIR"] = filepath.Join(dir, "automatic-"+secret)
				delete(env, "TRACEAI_JAVA_AGENT")
				if mode == "override" {
					jar := filepath.Join(dir, "trusted-"+secret, "agent.jar")
					data, err := os.ReadFile(filepath.Join(root, "java/traceai-javaagent/target/traceai-javaagent.jar"))
					if err != nil {
						t.Fatal(err)
					}
					put(t, jar, string(data))
					env["TRACEAI_JAVA_AGENT"] = jar
					env["TRACEAI_CACHE_DIR"] = filepath.Join(dir, "ordinary-cache")
				}
				original := environmentList(env)
				code, output, stderr := startupProbe(t, java, dir, original, "-version")
				actualCode, actualOut, actualErr := startupProbe(t, binary, dir, original, "run", "--", java, "-version")
				const warning = "traceai: Java agent path contains a known credential; injection skipped\n"
				if code != 0 || actualCode != code || actualOut != output || strings.ReplaceAll(actualErr, warning, "") != stderr || strings.Count(actualErr, warning) != 1 || strings.Contains(actualOut+actualErr, secret) || strings.Contains(actualErr, "-javaagent:") {
					t.Fatal("launcher introduced a credential into JVM diagnostics or changed native execution")
				}
			})
		}
	}
}
