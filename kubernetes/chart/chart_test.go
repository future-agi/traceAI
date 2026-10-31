package chart_test

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	admissionv1 "k8s.io/api/admissionregistration/v1"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	utilyaml "k8s.io/apimachinery/pkg/util/yaml"
)

func render(t *testing.T, extra ...string) ([]byte, error) {
	t.Helper()
	helm, err := exec.LookPath("helm")
	if err != nil {
		t.Skip("helm not installed")
	}
	args := append([]string{"template", "test", ".", "--namespace", "traceai-system", "--kubeconfig=/dev/null", "--kube-version=1.34.1"}, extra...)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, helm, args...)
	cache := t.TempDir()
	cmd.Env = append(os.Environ(),
		"HELM_CACHE_HOME="+filepath.Join(cache, "cache"),
		"HELM_CONFIG_HOME="+filepath.Join(cache, "config"),
		"HELM_DATA_HOME="+filepath.Join(cache, "data"),
		"HELM_PLUGINS="+filepath.Join(cache, "plugins"),
	)
	var diagnostics bytes.Buffer
	cmd.Stderr = &diagnostics
	out, err := cmd.Output()
	if err != nil {
		return append(out, diagnostics.Bytes()...), err
	}
	// Helm may warn about a local kubeconfig on stderr. Those diagnostics are
	// not YAML and must not contaminate the rendered-manifest decoder.
	return out, nil
}
func TestHelmRenderedSafetyContract(t *testing.T) {
	for _, tc := range []struct {
		name    string
		args    []string
		managed bool
	}{
		{"cert-manager", nil, true},
		{"literal-setting", []string{"--set-string", "settings.FI_PROJECT_NAME=literal-$(TOKEN)"}, true},
		{"existing-tls", []string{"--set", "certManager.enabled=false", "--set", "tls.existingSecret=external-tls", "--set", "tls.caBundle=Y2E="}, false},
		{"custom-issuer-digest", []string{"--set", "certManager.issuerRef.name=corporate-ca", "--set", "certManager.issuerRef.kind=ClusterIssuer", "--set", "image.digest=sha256:" + strings.Repeat("a", 64)}, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			out, err := render(t, tc.args...)
			if err != nil {
				t.Fatalf("helm render: %v\n%s", err, out)
			}
			reader := utilyaml.NewYAMLReader(bufio.NewReader(bytes.NewReader(out)))
			var deployment appsv1.Deployment
			var webhook admissionv1.MutatingWebhookConfiguration
			var role rbacv1.ClusterRole
			var service corev1.Service
			certificates, issuers, secrets := 0, 0, 0
			for {
				raw, err := reader.Read()
				if err == io.EOF {
					break
				}
				if err != nil {
					t.Fatal(err)
				}
				doc, err := utilyaml.ToJSON(raw)
				if err != nil {
					t.Fatal(err)
				}
				var meta metav1.TypeMeta
				if json.Unmarshal(doc, &meta) != nil {
					t.Fatal("invalid manifest")
				}
				switch meta.Kind {
				case "Deployment":
					if err := json.Unmarshal(doc, &deployment); err != nil {
						t.Fatal(err)
					}
				case "MutatingWebhookConfiguration":
					if err := json.Unmarshal(doc, &webhook); err != nil {
						t.Fatal(err)
					}
				case "ClusterRole":
					if err := json.Unmarshal(doc, &role); err != nil {
						t.Fatal(err)
					}
				case "Service":
					if err := json.Unmarshal(doc, &service); err != nil {
						t.Fatal(err)
					}
				case "Certificate":
					certificates++
				case "Issuer":
					issuers++
				case "Secret":
					secrets++
				}
			}
			if len(webhook.Webhooks) != 1 {
				t.Fatal("missing webhook")
			}
			w := webhook.Webhooks[0]
			if w.FailurePolicy == nil || *w.FailurePolicy != admissionv1.Ignore || w.TimeoutSeconds == nil || *w.TimeoutSeconds != 3 || w.SideEffects == nil || *w.SideEffects != admissionv1.SideEffectClassNone || len(w.AdmissionReviewVersions) != 1 || w.AdmissionReviewVersions[0] != "v1" {
				t.Fatal("admission availability contract violated")
			}
			if len(w.Rules) != 1 || len(w.Rules[0].Operations) != 1 || w.Rules[0].Operations[0] != admissionv1.Create || len(w.Rules[0].Resources) != 1 || w.Rules[0].Resources[0] != "pods" {
				t.Fatal("webhook scope is not CREATE pods only")
			}
			if len(w.NamespaceSelector.MatchExpressions) != 1 || !strings.Contains(strings.Join(w.NamespaceSelector.MatchExpressions[0].Values, ","), "traceai-system") {
				t.Fatal("operator namespace not excluded")
			}
			if w.ClientConfig.Service == nil || w.ClientConfig.Service.Namespace != "traceai-system" || w.ClientConfig.Service.Name != service.Name || *w.ClientConfig.Service.Port != 443 || *w.ClientConfig.Service.Path != "/mutate" {
				t.Fatal("wrong webhook endpoint")
			}
			if secrets != 0 || len(role.Rules) != 1 || strings.Join(role.Rules[0].Resources, ",") != "namespaces" || strings.Join(role.Rules[0].Verbs, ",") != "get" {
				t.Fatal("credential access or overbroad RBAC")
			}
			if deployment.Spec.Replicas == nil || *deployment.Spec.Replicas != 2 || deployment.Spec.Template.Annotations["traceai.futureagi.com/inject"] != "false" {
				t.Fatal("unsafe deployment defaults")
			}
			container := deployment.Spec.Template.Spec.Containers[0]
			if !strings.Contains(strings.Join(container.Args, " "), "--agent-image="+container.Image) || !*container.SecurityContext.ReadOnlyRootFilesystem || *container.SecurityContext.AllowPrivilegeEscalation {
				t.Fatal("unsafe image/init deployment")
			}
			if tc.name == "custom-issuer-digest" && !strings.Contains(container.Image, "@sha256:") {
				t.Fatal("digest not honored")
			}
			if tc.name == "literal-setting" && !strings.Contains(container.Env[1].Value, "literal-$$(TOKEN)") {
				t.Fatal("settings can expand another webhook environment variable")
			}
			if tc.managed {
				if certificates != 1 || webhook.Annotations["cert-manager.io/inject-ca-from"] == "" {
					t.Fatal("CA injection missing")
				}
			} else {
				if certificates != 0 || issuers != 0 || string(w.ClientConfig.CABundle) != "ca" {
					t.Fatal("external TLS not honored")
				}
			}
		})
	}
}

func TestHelmRejectsUnsafeValues(t *testing.T) {
	for _, tc := range []struct {
		name string
		args []string
	}{
		{"missing-external-tls", []string{"--set", "certManager.enabled=false"}},
		{"inline-credential", []string{"--set-string", "settings.FI_API_KEY=forbidden"}},
		{"runtime-override", []string{"--set-string", "settings.NODE_OPTIONS=forbidden"}},
		{"config-path-override", []string{"--set-string", "settings.FI_CONFIG_FILE=forbidden"}},
		{"non-string-setting", []string{"--set", "settings.FI_HIDE_INPUTS=true"}},
		{"zero-replicas", []string{"--set", "replicaCount=0"}},
		{"invalid-digest", []string{"--set", "image.digest=not-a-digest"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := render(t, tc.args...); err == nil {
				t.Fatal("unsafe chart values accepted")
			}
		})
	}
}
