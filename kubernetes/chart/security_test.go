package chart_test

import (
	"bytes"
	"encoding/json"
	"io"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	utilyaml "k8s.io/apimachinery/pkg/util/yaml"
)

func securityResources(t *testing.T, args ...string) (appsv1.Deployment, *networkingv1.NetworkPolicy) {
	t.Helper()
	out, err := render(t, args...)
	if err != nil {
		t.Fatalf("helm render: %v\n%s", err, out)
	}
	decoder := utilyaml.NewYAMLOrJSONDecoder(bytes.NewReader(out), 4096)
	var deployment appsv1.Deployment
	var policy *networkingv1.NetworkPolicy
	for {
		var raw json.RawMessage
		if err := decoder.Decode(&raw); err == io.EOF {
			break
		} else if err != nil {
			t.Fatal(err)
		}
		var meta metav1.TypeMeta
		if err := json.Unmarshal(raw, &meta); err != nil {
			t.Fatal(err)
		}
		switch meta.Kind {
		case "Deployment":
			if err := json.Unmarshal(raw, &deployment); err != nil {
				t.Fatal(err)
			}
		case "NetworkPolicy":
			if policy != nil {
				t.Fatal("duplicate ingress policy")
			}
			policy = &networkingv1.NetworkPolicy{}
			if err := json.Unmarshal(raw, policy); err != nil {
				t.Fatal(err)
			}
		}
	}
	return deployment, policy
}

func TestHelmControlPlaneIngress(t *testing.T) {
	for _, tc := range []struct {
		name           string
		args           []string
		ingress, peers int
		disabled       bool
	}{
		{"default-deny", nil, 0, 0, false},
		{"api-server-cidrs", []string{"--set-json", `networkPolicy.apiServerCIDRs=["192.0.2.10/32","2001:db8::10/128"]`}, 1, 2, false},
		{"control-plane-selectors", []string{"--set-json", `networkPolicy.controlPlanePeers=[{"namespaceSelector":{"matchLabels":{"kubernetes.io/metadata.name":"kube-system"}},"podSelector":{"matchLabels":{"component":"kube-apiserver"}}}]`}, 1, 1, false},
		{"external-policy-off-switch", []string{"--set", "networkPolicy.enabled=false"}, 0, 0, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			deployment, policy := securityResources(t, tc.args...)
			if tc.disabled {
				if policy != nil {
					t.Fatal("off switch ignored")
				}
				return
			}
			if policy == nil || policy.Namespace != "" && policy.Namespace != "traceai-system" || len(policy.Spec.PolicyTypes) != 1 || policy.Spec.PolicyTypes[0] != networkingv1.PolicyTypeIngress {
				t.Fatal("missing ingress-only policy")
			}
			if len(policy.Spec.PodSelector.MatchLabels) == 0 {
				t.Fatal("policy selects every Pod")
			}
			for k, v := range policy.Spec.PodSelector.MatchLabels {
				if deployment.Spec.Template.Labels[k] != v {
					t.Fatal("policy does not select webhook")
				}
			}
			if len(policy.Spec.Ingress) != tc.ingress {
				t.Fatal("wrong ingress allowlist")
			}
			for _, rule := range policy.Spec.Ingress {
				if len(rule.Ports) != 1 || rule.Ports[0].Port == nil || rule.Ports[0].Port.IntVal != 8443 || len(rule.From) != tc.peers {
					t.Fatal("policy allows incorrect ports/peers")
				}
				for _, peer := range rule.From {
					if peer.IPBlock == nil && (peer.NamespaceSelector == nil || len(peer.NamespaceSelector.MatchLabels) == 0 || peer.PodSelector == nil || len(peer.PodSelector.MatchLabels) == 0) {
						t.Fatal("selector peer permits arbitrary workloads")
					}
				}
			}
		})
	}
}

func TestHelmClientAuthenticationAndResourceWiring(t *testing.T) {
	deployment, _ := securityResources(t, "--set", "tls.clientCAConfigMap=dedicated-apiserver-ca", "--set", "tls.clientCAKey=clients.pem", "--set", "admission.maxConcurrentAdmissions=4", "--set", "admission.maxConcurrentLookups=2", "--set-string", "initResources.limits.cpu=250m", "--set-string", "initResources.limits.memory=256Mi")
	c := deployment.Spec.Template.Spec.Containers[0]
	args := strings.Join(c.Args, " ")
	for _, want := range []string{"--tls-client-ca=/client-ca/ca.crt", "--max-concurrent-admissions=4", "--max-concurrent-lookups=2", `"cpu":"250m"`, `"memory":"256Mi"`} {
		if !strings.Contains(args, want) {
			t.Fatalf("missing deployment arg %s", want)
		}
	}
	if c.ReadinessProbe.TCPSocket == nil || c.ReadinessProbe.HTTPGet != nil || c.LivenessProbe.TCPSocket == nil || c.LivenessProbe.HTTPGet != nil {
		t.Fatal("kubelet probes require unavailable API-server certificate")
	}
	foundVolume, foundMount := false, false
	for _, v := range deployment.Spec.Template.Spec.Volumes {
		if v.Name == "client-ca" {
			foundVolume = v.ConfigMap != nil && v.ConfigMap.Name == "dedicated-apiserver-ca" && len(v.ConfigMap.Items) == 1 && v.ConfigMap.Items[0].Key == "clients.pem" && v.ConfigMap.Items[0].Path == "ca.crt"
		}
	}
	for _, m := range c.VolumeMounts {
		if m.Name == "client-ca" {
			foundMount = m.ReadOnly && m.MountPath == "/client-ca" && m.SubPath == ""
		}
	}
	if !foundVolume || !foundMount {
		t.Fatal("client CA rotation-safe mount missing")
	}
	plain, _ := securityResources(t)
	if plain.Spec.Template.Spec.Containers[0].ReadinessProbe.HTTPGet == nil {
		t.Fatal("server-only TLS probes changed")
	}
}

func TestHelmRejectsUnsafeControlPlaneValues(t *testing.T) {
	for _, tc := range []struct {
		name string
		args []string
	}{
		{"zero-admissions", []string{"--set", "admission.maxConcurrentAdmissions=0"}},
		{"too-many-admissions", []string{"--set", "admission.maxConcurrentAdmissions=1025"}},
		{"zero-lookups", []string{"--set", "admission.maxConcurrentLookups=0"}},
		{"lookups-over-admission-budget", []string{"--set", "admission.maxConcurrentAdmissions=2"}},
		{"wildcard-control-plane-peer", []string{"--set-json", `networkPolicy.controlPlanePeers=[{}]`}},
		{"namespace-only-peer", []string{"--set-json", `networkPolicy.controlPlanePeers=[{"namespaceSelector":{"matchLabels":{"name":"system"}}}]`}},
		{"empty-selector", []string{"--set-json", `networkPolicy.controlPlanePeers=[{"namespaceSelector":{"matchLabels":{}},"podSelector":{"matchLabels":{"component":"apiserver"}}}]`}},
		{"not-cidr", []string{"--set-json", `networkPolicy.apiServerCIDRs=["not-a-cidr"]`}},
		{"invalid-quantity", []string{"--set-string", "initResources.requests.cpu=invalid"}},
		{"negative-quantity", []string{"--set-string", "initResources.requests.memory=-1Mi"}},
		{"injected-resource-claim", []string{"--set-json", `initResources.claims=[{"name":"unsupported"}]`}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if out, err := render(t, tc.args...); err == nil {
				t.Fatalf("unsafe chart configuration accepted\n%s", out)
			}
		})
	}
}
