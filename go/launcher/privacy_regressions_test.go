package launcher

import (
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"testing"
)

func TestCredentialRedactionCoversNodeOptionEscapes(t *testing.T) {
	for _, secret := range []string{`VERIFY_NODE_PLAIN_SECRET_9032`, `VERIFY_NODE_"QUOTED"_SECRET_9032`, `VERIFY_NODE_\BACKSLASH_SECRET_9032`, "VERIFY_NODE_\t\"\\_SECRET_9032"} {
		t.Run(strconv.Quote(secret), func(t *testing.T) {
			sources := map[string]string{"FI_API_KEY": secret}
			for _, encoded := range []string{secret, quoteNodeOption(secret), strconv.Quote(secret)} {
				values := map[string]string{"value": "before/" + encoded + "/after"}
				before := values["value"]
				masked := Masked(values, sources)["value"]
				if !strings.Contains(masked, "***") || strings.Contains(masked, "SECRET_9032") {
					t.Fatal("display masking disclosed a reversibly escaped credential")
				}
				if diagnostic := redactText(before, environmentList(sources)); diagnostic != masked {
					t.Fatal("diagnostic redaction and display masking disagree")
				}
				if values["value"] != before {
					t.Fatal("masking mutated the child value")
				}
			}
		})
	}
}

func TestAuthorizationHeadersAreCredentialContext(t *testing.T) {
	for _, key := range []string{"HTTP_AUTHORIZATION", "Authorization", "PROXY_AUTHORIZATION", "http_proxy_authorization"} {
		t.Run(key, func(t *testing.T) {
			const token = "VERIFY_HEADER_CREDENTIAL_9032"
			const header = "Bearer " + token
			sources := map[string]string{key: header}
			for _, value := range []string{header, token} {
				masked := Masked(map[string]string{"FI_PROJECT_NAME": value, key: header}, sources)
				if masked["FI_PROJECT_NAME"] != "***" || masked[key] != "***" || redactText(value, environmentList(sources)) != "***" {
					t.Fatal("authorization credentials were not redacted")
				}
			}
		})
	}
}

func TestJavaCredentialBearingAgentPathFailsOpen(t *testing.T) {
	for _, sensitive := range []bool{true, false} {
		t.Run(strconv.FormatBool(sensitive), func(t *testing.T) {
			dir := t.TempDir()
			name := "ordinary-agent"
			if sensitive {
				name = "VERIFY_JAVA_AGENT_PATH_SECRET_9032"
			}
			jar := filepath.Join(dir, name, "agent.jar")
			put(t, jar, "trusted jar fixture")
			env := environmentMap(baseEnv(t))
			env["FI_API_KEY"] = "VERIFY_JAVA_AGENT_PATH_SECRET_9032"
			env["TRACEAI_JAVA_AGENT"] = jar
			env["JAVA_TOOL_OPTIONS"] = "-Xmx128m"
			env["OTEL_SDK_DISABLED"] = "true"
			original := environmentList(env)
			plan := Prepare([]string{"java", "-version"}, original, dir, Options{Runtimes: []string{"java"}})
			actual := environmentMap(plan.Environment)
			if sensitive {
				if actual["JAVA_TOOL_OPTIONS"] != env["JAVA_TOOL_OPTIONS"] || plan.Changes["JAVA_TOOL_OPTIONS"] != "" || len(plan.Warnings) != 1 || !strings.Contains(plan.Warnings[0], "credential") || strings.Contains(plan.Warnings[0], name) {
					t.Fatal("credential-bearing injection was not declined safely")
				}
			} else if !strings.Contains(actual["JAVA_TOOL_OPTIONS"], jar) || !strings.HasSuffix(actual["JAVA_TOOL_OPTIONS"], env["JAVA_TOOL_OPTIONS"]) || len(plan.Warnings) != 0 {
				t.Fatal("ordinary JVM injection changed")
			}
			if !reflect.DeepEqual(original, environmentList(env)) || actual["FI_API_KEY"] != env["FI_API_KEY"] || !readableFile(jar) {
				t.Fatal("privacy check altered the application or caller artifact")
			}
			if _, err := os.Stat(jar); err != nil {
				t.Fatal(err)
			}
		})
	}
}
