package injector

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	admissionv1 "k8s.io/api/admission/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
)

type lookupFunc func(context.Context, string) (bool, error)

func (f lookupFunc) Enabled(ctx context.Context, ns string) (bool, error) { return f(ctx, ns) }
func admissionRequest(t *testing.T) admissionv1.AdmissionReview {
	t.Helper()
	return admissionv1.AdmissionReview{TypeMeta: metav1.TypeMeta{APIVersion: "admission.k8s.io/v1", Kind: "AdmissionReview"}, Request: &admissionv1.AdmissionRequest{UID: types.UID("test-uid"), Kind: metav1.GroupVersionKind{Version: "v1", Kind: "Pod"}, Resource: metav1.GroupVersionResource{Version: "v1", Resource: "pods"}, Namespace: "apps", Operation: admissionv1.Create, Object: runtime.RawExtension{Raw: rawPod(t, testPod("node"))}}}
}
func serve(t *testing.T, h http.Handler, review admissionv1.AdmissionReview) *admissionv1.AdmissionResponse {
	t.Helper()
	data, err := json.Marshal(review)
	if err != nil {
		t.Fatal(err)
	}
	recorder := httptest.NewRecorder()
	h.ServeHTTP(recorder, httptest.NewRequest(http.MethodPost, "/mutate", bytes.NewReader(data)))
	if recorder.Code != 200 {
		t.Fatalf("HTTP %d", recorder.Code)
	}
	var out admissionv1.AdmissionReview
	if err := json.Unmarshal(recorder.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	if out.Response == nil || !out.Response.Allowed || out.Response.UID != review.Request.UID {
		t.Fatal("invalid fail-open response")
	}
	return out.Response
}

func TestInProcessTLSWebhook(t *testing.T) {
	server := httptest.NewTLSServer(Handler{Config: testConfig()})
	defer server.Close()
	review := admissionRequest(t)
	data, _ := json.Marshal(review)
	resp, err := server.Client().Post(server.URL, "application/json", bytes.NewReader(data))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out admissionv1.AdmissionReview
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	if out.Response == nil || !out.Response.Allowed || out.Response.UID != review.Request.UID || out.Response.PatchType == nil || *out.Response.PatchType != admissionv1.PatchTypeJSONPatch {
		t.Fatal("invalid AdmissionReview")
	}
	var patch []Patch
	if err := json.Unmarshal(out.Response.Patch, &patch); err != nil {
		t.Fatal(err)
	}
	pod, _ := apply(t, review.Request.Object.Raw, patch)
	value(t, pod.Spec.Containers[0], "NODE_OPTIONS")
}

func TestAdmissionNamespaceLookupAndFailOpen(t *testing.T) {
	for _, tc := range []struct {
		name                                        string
		opt                                         *string
		enabled, fail, panicLookup, mutated, called bool
	}{
		{"annotation-skips-lookup", ptr("true"), false, false, false, true, false},
		{"opt-out-skips-lookup", ptr("false"), true, false, false, false, false},
		{"namespace-enabled", nil, true, false, false, true, true},
		{"namespace-disabled", nil, false, false, false, false, true},
		{"namespace-unavailable", nil, false, true, false, false, true},
		{"lookup-panics", nil, false, false, true, false, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			called := false
			h := Handler{Config: testConfig(), Namespaces: lookupFunc(func(ctx context.Context, ns string) (bool, error) {
				called = true
				if ns != "apps" {
					t.Fatal("wrong namespace")
				}
				deadline, ok := ctx.Deadline()
				if !ok || time.Until(deadline) > 310*time.Millisecond {
					t.Fatal("unbounded lookup")
				}
				if tc.panicLookup {
					panic("secret-do-not-print")
				}
				if tc.fail {
					return false, fmt.Errorf("secret-do-not-print")
				}
				return tc.enabled, nil
			})}
			review := admissionRequest(t)
			p := testPod("node")
			delete(p.Annotations, Inject)
			if tc.opt != nil {
				p.Annotations[Inject] = *tc.opt
			}
			p.Namespace = ""
			review.Request.Object.Raw = rawPod(t, p)
			response := serve(t, h, review)
			if (len(response.Patch) > 0) != tc.mutated || called != tc.called {
				t.Fatalf("mutated=%t called=%t", len(response.Patch) > 0, called)
			}
			data, _ := json.Marshal(response)
			if bytes.Contains(data, []byte("secret-do-not-print")) {
				t.Fatal("error contents leaked")
			}
		})
	}
}
func ptr(s string) *string { return &s }

func TestAdmissionReservedApplicationContainerName(t *testing.T) {
	for _, optIn := range []string{"annotation", "namespace"} {
		t.Run(optIn, func(t *testing.T) {
			p := testPod("node")
			p.Spec.Containers[0].Name = InitName
			if optIn == "namespace" {
				delete(p.Annotations, Inject)
			}
			review := admissionRequest(t)
			review.Request.Object.Raw = rawPod(t, p)
			h := Handler{Config: testConfig(), Namespaces: lookupFunc(func(context.Context, string) (bool, error) {
				return true, nil
			})}
			response := serve(t, h, review)
			if len(response.Patch) != 0 || response.PatchType != nil {
				t.Fatal("reserved application name must be admitted without any mutation")
			}
		})
	}
}

func TestAdmissionDryRunAndDeterministicPatch(t *testing.T) {
	h := Handler{Config: testConfig()}
	review := admissionRequest(t)
	first := serve(t, h, review)
	dryRun := true
	review.Request.DryRun = &dryRun
	second := serve(t, h, review)
	if len(first.Patch) == 0 || !bytes.Equal(first.Patch, second.Patch) {
		t.Fatal("dry-run did not produce the same deterministic, side-effect-free patch")
	}
}

func TestNamespaceLookupDeadlineFailsOpen(t *testing.T) {
	canceled := make(chan struct{})
	h := Handler{Config: testConfig(), Namespaces: lookupFunc(func(ctx context.Context, _ string) (bool, error) {
		<-ctx.Done()
		close(canceled)
		return false, ctx.Err()
	})}
	review := admissionRequest(t)
	p := testPod("node")
	delete(p.Annotations, Inject)
	review.Request.Object.Raw = rawPod(t, p)
	response := serve(t, h, review)
	select {
	case <-canceled:
	case <-time.After(time.Second):
		t.Fatal("namespace worker did not observe cancellation")
	}
	if len(response.Patch) != 0 {
		t.Fatal("namespace deadline did not leave Pod creation untouched")
	}
}

func TestAdmissionFiltersAndInvalidInputs(t *testing.T) {
	for _, tc := range []struct {
		name   string
		change func(*admissionv1.AdmissionReview)
	}{
		{"update", func(r *admissionv1.AdmissionReview) { r.Request.Operation = admissionv1.Update }},
		{"delete", func(r *admissionv1.AdmissionReview) { r.Request.Operation = admissionv1.Delete }},
		{"subresource", func(r *admissionv1.AdmissionReview) { r.Request.SubResource = "ephemeralcontainers" }},
		{"wrong-resource", func(r *admissionv1.AdmissionReview) { r.Request.Resource.Resource = "deployments" }},
		{"wrong-kind", func(r *admissionv1.AdmissionReview) { r.Request.Kind.Kind = "Service" }},
		{"wrong-version", func(r *admissionv1.AdmissionReview) { r.APIVersion = "admission.k8s.io/v1beta1" }},
		{"excluded", func(r *admissionv1.AdmissionReview) { r.Request.Namespace = "kube-system" }},
		{"no-namespace", func(r *admissionv1.AdmissionReview) { r.Request.Namespace = "" }},
		{"namespace-mismatch", func(r *admissionv1.AdmissionReview) { r.Request.Namespace = "other" }},
		{"invalid-object", func(r *admissionv1.AdmissionReview) { r.Request.Object.Raw = []byte(`{"spec":"invalid"}`) }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := admissionRequest(t)
			tc.change(&r)
			response := serve(t, Handler{Config: testConfig()}, r)
			if len(response.Patch) > 0 {
				t.Fatal("unexpected patch")
			}
		})
	}
	for _, body := range []string{"not-json", `{}`, `{"request":null}`, strings.Repeat("x", MaxRequestBytes+1)} {
		w := httptest.NewRecorder()
		Handler{Config: testConfig()}.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/mutate", strings.NewReader(body)))
		var r admissionv1.AdmissionReview
		if json.Unmarshal(w.Body.Bytes(), &r) != nil || r.Response == nil || !r.Response.Allowed || len(r.Response.Patch) > 0 {
			t.Fatal("invalid input did not fail open")
		}
	}
	w := httptest.NewRecorder()
	Handler{}.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/mutate", nil))
	if w.Code != http.StatusMethodNotAllowed {
		t.Fatal("GET must not mutate")
	}
	r := admissionRequest(t)
	cfg := testConfig()
	cfg.Settings["FI_SECRET_KEY"] = "secret-never-log"
	response := serve(t, Handler{Config: cfg}, r)
	if len(response.Patch) > 0 {
		t.Fatal("invalid config mutated")
	}
	data, _ := json.Marshal(response)
	if bytes.Contains(data, []byte("secret-never-log")) {
		t.Fatal("configuration leaked")
	}
}

func TestNamespaceClientTokenRotationAndFailures(t *testing.T) {
	dir := t.TempDir()
	tokenFile := filepath.Join(dir, "token")
	if err := os.WriteFile(tokenFile, []byte("first-token"), 0600); err != nil {
		t.Fatal(err)
	}
	status := 200
	body := `{"metadata":{"name":"apps","labels":{"traceai.futureagi.com/inject":"true"}}}`
	token := "first-token"
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/namespaces/apps" || r.Header.Get("Authorization") != "Bearer "+token {
			t.Error("wrong namespace request/token")
		}
		w.WriteHeader(status)
		_, _ = io.WriteString(w, body)
	}))
	defer server.Close()
	client := &NamespaceClient{Client: server.Client(), BaseURL: server.URL, TokenFile: tokenFile}
	for _, rotated := range []string{"first-token", "rotated-token"} {
		token = rotated
		if err := os.WriteFile(tokenFile, []byte(rotated+"\n"), 0600); err != nil {
			t.Fatal(err)
		}
		enabled, err := client.Enabled(context.Background(), "apps")
		if err != nil || !enabled {
			t.Fatalf("lookup failed: %v", err)
		}
	}
	status = 403
	if enabled, err := client.Enabled(context.Background(), "apps"); enabled || err == nil {
		t.Fatal("RBAC denial must disable injection")
	}
	status = 200
	body = `{"metadata":{"name":"different"}}`
	if enabled, err := client.Enabled(context.Background(), "apps"); enabled || err == nil {
		t.Fatal("wrong namespace accepted")
	}
	body = strings.Repeat("x", (1<<20)+1)
	if _, err := client.Enabled(context.Background(), "apps"); err == nil {
		t.Fatal("unbounded response accepted")
	}
	body = `not-json`
	if _, err := client.Enabled(context.Background(), "apps"); err == nil {
		t.Fatal("malformed response accepted")
	}
	body = `{"metadata":{"name":"apps"}}`
	if enabled, err := client.Enabled(context.Background(), "apps"); enabled || err != nil {
		t.Fatal("unlabeled namespace enabled")
	}
	client.BaseURL = "http://not-tls.invalid"
	if _, err := client.Enabled(context.Background(), "apps"); err == nil {
		t.Fatal("plaintext API accepted")
	}
}

func TestTLSConfigCertificateReload(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	defer server.Close()
	cert := server.TLS.Certificates[0]
	dir := t.TempDir()
	certFile, keyFile := filepath.Join(dir, "tls.crt"), filepath.Join(dir, "tls.key")
	writePair := func(c tls.Certificate) {
		t.Helper()
		if err := os.WriteFile(certFile, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: c.Certificate[0]}), 0644); err != nil {
			t.Fatal(err)
		}
		key, err := x509.MarshalPKCS8PrivateKey(c.PrivateKey)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(keyFile, pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: key}), 0600); err != nil {
			t.Fatal(err)
		}
	}
	writePair(cert)
	cfg, err := TLSConfig(certFile, keyFile)
	if err != nil || cfg.MinVersion != tls.VersionTLS12 {
		t.Fatalf("bad TLS config: %v", err)
	}
	got, err := cfg.GetCertificate(&tls.ClientHelloInfo{})
	if err != nil || !bytes.Equal(got.Certificate[0], cert.Certificate[0]) {
		t.Fatal("cert not loaded")
	}
	if err := os.WriteFile(keyFile, []byte("broken key"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := cfg.GetCertificate(&tls.ClientHelloInfo{}); err == nil {
		t.Fatal("stale certificate used instead of reloading")
	}
	if _, err := TLSConfig(certFile, keyFile); err == nil {
		t.Fatal("invalid initial pair accepted")
	}
	writePair(cert)
	if _, err := cfg.GetCertificate(&tls.ClientHelloInfo{}); err != nil {
		t.Fatal("repaired certificate not reloaded")
	}
}
