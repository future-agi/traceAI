package injector

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	admissionv1 "k8s.io/api/admission/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

const MaxRequestBytes = 4 << 20

type NamespaceLookup interface {
	Enabled(context.Context, string) (bool, error)
}

// AdmissionLimits are shared by all requests to a handler. Acquires never wait:
// saturation admits an unmodified Pod rather than queueing control-plane work.
// Lookup slots remain held until their worker exits, even after a timeout.
type AdmissionLimits struct {
	admissions chan struct{}
	lookups    chan struct{}
}

func NewAdmissionLimits(admissions, lookups int) (*AdmissionLimits, error) {
	if admissions < 1 || admissions > 1024 || lookups < 1 || lookups > admissions {
		return nil, fmt.Errorf("invalid admission concurrency limits")
	}
	return &AdmissionLimits{admissions: make(chan struct{}, admissions), lookups: make(chan struct{}, lookups)}, nil
}

var defaultLimits, _ = NewAdmissionLimits(32, 8)

const admissionTimeout = time.Second
const namespaceTimeout = 300 * time.Millisecond

func acquire(slots chan struct{}) bool {
	select {
	case slots <- struct{}{}:
		return true
	default:
		return false
	}
}

type Handler struct {
	Config     Config
	Namespaces NamespaceLookup
	Limits     *AdmissionLimits
}

func (h Handler) lookup(ctx context.Context, limits *AdmissionLimits, namespace string) (bool, error) {
	if !acquire(limits.lookups) {
		return false, fmt.Errorf("namespace lookup overloaded")
	}
	type result struct {
		enabled bool
		err     error
	}
	done := make(chan result, 1)
	go func() {
		var out result
		defer func() {
			if recover() != nil {
				out = result{err: fmt.Errorf("namespace lookup failed")}
			}
			<-limits.lookups
			done <- out
		}()
		out.enabled, out.err = h.Namespaces.Enabled(ctx, namespace)
	}()
	select {
	case out := <-done:
		if ctx.Err() != nil {
			return false, fmt.Errorf("namespace lookup timed out")
		}
		return out.enabled, out.err
	case <-ctx.Done():
		return false, fmt.Errorf("namespace lookup timed out")
	}
}

// Under overload, do not retain/decode the entire Pod outside the admission
// budget. Kubernetes serializes request.uid before the Pod. Probe at most 64 KiB
// to echo it; an absent/unreadable UID is a webhook failure (failurePolicy=Ignore).
func overloadUID(reader io.Reader) types.UID {
	decoder := json.NewDecoder(io.LimitReader(reader, 64<<10))
	start, err := decoder.Token()
	if err != nil || start != json.Delim('{') {
		return ""
	}
	for decoder.More() {
		key, err := decoder.Token()
		if err != nil {
			return ""
		}
		if key != "request" {
			var ignored json.RawMessage
			if decoder.Decode(&ignored) != nil {
				return ""
			}
			continue
		}
		start, err = decoder.Token()
		if err != nil || start != json.Delim('{') {
			return ""
		}
		for decoder.More() {
			key, err = decoder.Token()
			if err != nil {
				return ""
			}
			if key == "uid" {
				var uid types.UID
				if decoder.Decode(&uid) == nil {
					return uid
				}
				return ""
			}
			var ignored json.RawMessage
			if decoder.Decode(&ignored) != nil {
				return ""
			}
		}
		return ""
	}
	return ""
}

func (h Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	limits := h.Limits
	if limits == nil {
		limits = defaultLimits
	}
	admitted := acquire(limits.admissions)
	if admitted {
		defer func() { <-limits.admissions }()
	}
	ctx, cancel := context.WithTimeout(r.Context(), admissionTimeout)
	defer cancel()
	// Bound slow bodies as well as API calls. The production HTTP server supports
	// this controller; in-process recorders may not. Server timeouts are a backstop.
	_ = http.NewResponseController(w).SetReadDeadline(time.Now().Add(admissionTimeout))
	response := &admissionv1.AdmissionResponse{Allowed: true}
	// Never serialize the request or errors: both can contain application secrets.
	defer func() {
		if recover() != nil {
			response.Patch = nil
			response.PatchType = nil
			response.Warnings = []string{"traceAI injection skipped: internal error"}
		}
		if ctx.Err() != nil {
			response.Patch, response.PatchType = nil, nil
			response.Warnings = []string{"traceAI injection skipped: admission timed out"}
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(admissionv1.AdmissionReview{TypeMeta: metav1.TypeMeta{APIVersion: "admission.k8s.io/v1", Kind: "AdmissionReview"}, Response: response})
	}()
	reader := http.MaxBytesReader(w, r.Body, MaxRequestBytes)
	defer reader.Close()
	if !admitted {
		response.UID = overloadUID(reader)
		response.Warnings = []string{"traceAI injection skipped: admission overloaded"}
		return
	}
	var review admissionv1.AdmissionReview
	data, err := io.ReadAll(reader)
	if err != nil || json.Unmarshal(data, &review) != nil || review.Request == nil {
		response.Warnings = []string{"traceAI injection skipped: invalid admission request"}
		return
	}
	req := review.Request
	response.UID = req.UID
	if ctx.Err() != nil {
		return
	}
	if review.APIVersion != "admission.k8s.io/v1" || review.Kind != "AdmissionReview" || req.Operation != admissionv1.Create || req.Resource.Group != "" || req.Resource.Version != "v1" || req.Resource.Resource != "pods" || req.SubResource != "" || req.Kind.Group != "" || req.Kind.Version != "v1" || req.Kind.Kind != "Pod" {
		return
	}
	if req.Namespace == "" || contains(h.Config.ExcludedNamespaces, req.Namespace) {
		return
	}
	var meta struct {
		Metadata metav1.ObjectMeta `json:"metadata"`
	}
	if json.Unmarshal(req.Object.Raw, &meta) != nil || meta.Metadata.Namespace != "" && meta.Metadata.Namespace != req.Namespace {
		return
	}
	namespaceEnabled := false
	if opt, exists := meta.Metadata.Annotations[Inject]; exists {
		if opt != "true" {
			return
		}
	} else {
		if h.Namespaces == nil {
			return
		}
		lookupCtx, cancelLookup := context.WithTimeout(ctx, namespaceTimeout)
		defer cancelLookup()
		namespaceEnabled, err = h.lookup(lookupCtx, limits, req.Namespace)
		if err != nil {
			response.Warnings = []string{"traceAI injection skipped: namespace lookup unavailable or overloaded"}
			return
		}
		if !namespaceEnabled {
			return
		}
	}
	patch, err := Mutation(req.Object.Raw, namespaceEnabled, h.Config)
	if err != nil {
		response.Warnings = []string{"traceAI injection skipped: invalid or unsupported configuration"}
		return
	}
	if len(patch) == 0 {
		return
	}
	response.Patch, err = json.Marshal(patch)
	if err != nil {
		response.Patch = nil
		return
	}
	kind := admissionv1.PatchTypeJSONPatch
	response.PatchType = &kind
}

// NamespaceClient gets only namespace labels, never Secrets or ConfigMaps. The
// token is reread on every request to support projected-token rotation. A failed
// lookup disables injection, not Pod creation. No redirects or insecure TLS.
type NamespaceClient struct {
	Client    *http.Client
	BaseURL   string
	TokenFile string
}

func InClusterNamespaces() (*NamespaceClient, error) {
	const account = "/var/run/secrets/kubernetes.io/serviceaccount/"
	host := os.Getenv("KUBERNETES_SERVICE_HOST")
	port := os.Getenv("KUBERNETES_SERVICE_PORT_HTTPS")
	if host == "" {
		return nil, fmt.Errorf("in-cluster API unavailable")
	}
	if port == "" {
		port = "443"
	}
	ca, err := os.ReadFile(account + "ca.crt")
	if err != nil {
		return nil, fmt.Errorf("API CA unavailable")
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(ca) {
		return nil, fmt.Errorf("invalid API CA")
	}
	transport := &http.Transport{TLSClientConfig: &tls.Config{RootCAs: roots, MinVersion: tls.VersionTLS12}, DialContext: (&net.Dialer{Timeout: 200 * time.Millisecond, KeepAlive: 30 * time.Second}).DialContext, TLSHandshakeTimeout: 200 * time.Millisecond, MaxIdleConns: 8, MaxIdleConnsPerHost: 8, MaxConnsPerHost: 8, IdleConnTimeout: 30 * time.Second, ResponseHeaderTimeout: 250 * time.Millisecond}
	client := &http.Client{Transport: transport, Timeout: 300 * time.Millisecond, CheckRedirect: func(*http.Request, []*http.Request) error { return fmt.Errorf("API redirects disabled") }}
	return &NamespaceClient{Client: client, BaseURL: "https://" + net.JoinHostPort(host, port), TokenFile: account + "token"}, nil
}

func (c *NamespaceClient) Enabled(ctx context.Context, namespace string) (bool, error) {
	if c.Client == nil || !strings.HasPrefix(c.BaseURL, "https://") {
		return false, fmt.Errorf("invalid API client")
	}
	token, err := os.ReadFile(c.TokenFile)
	if err != nil {
		return false, fmt.Errorf("API token unavailable")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+"/api/v1/namespaces/"+url.PathEscape(namespace), nil)
	if err != nil {
		return false, fmt.Errorf("invalid API address")
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(string(token)))
	req.Header.Set("Accept", "application/json")
	resp, err := c.Client.Do(req)
	if err != nil {
		return false, fmt.Errorf("namespace lookup unavailable")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return false, fmt.Errorf("namespace lookup unavailable")
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20+1))
	if err != nil || len(data) > 1<<20 {
		return false, fmt.Errorf("namespace response too large")
	}
	var ns corev1.Namespace
	if json.Unmarshal(data, &ns) != nil || ns.Name != namespace {
		return false, fmt.Errorf("invalid namespace response")
	}
	return ns.Labels[Inject] == "true", nil
}

// Reload TLS on every handshake so cert-manager's atomic Secret-volume rotation
// needs no restart. Load a complete pair before serving to catch bad deployment.
func TLSConfig(certFile, keyFile string) (*tls.Config, error) {
	return TLSConfigWithClientCA(certFile, keyFile, "")
}

// A dedicated API-server client CA authenticates callers when the cluster can
// configure webhook client certificates. Never trust identity headers from Pods.
// Both serving certificates and client trust roots reload for every handshake.
func TLSConfigWithClientCA(certFile, keyFile, clientCAFile string) (*tls.Config, error) {
	if _, err := tls.LoadX509KeyPair(certFile, keyFile); err != nil {
		return nil, fmt.Errorf("serving certificate unavailable")
	}
	cfg := &tls.Config{MinVersion: tls.VersionTLS12, GetCertificate: func(*tls.ClientHelloInfo) (*tls.Certificate, error) {
		cert, err := tls.LoadX509KeyPair(certFile, keyFile)
		if err != nil {
			return nil, fmt.Errorf("serving certificate unavailable")
		}
		return &cert, nil
	}}
	if clientCAFile != "" {
		loadRoots := func() (*x509.CertPool, error) {
			data, err := os.ReadFile(clientCAFile)
			if err != nil {
				return nil, fmt.Errorf("client CA unavailable")
			}
			roots := x509.NewCertPool()
			if !roots.AppendCertsFromPEM(data) {
				return nil, fmt.Errorf("invalid client CA")
			}
			return roots, nil
		}
		roots, err := loadRoots()
		if err != nil {
			return nil, err
		}
		cfg.ClientAuth, cfg.ClientCAs = tls.RequireAndVerifyClientCert, roots
		// Reauthenticate every new connection against the current CA after rotation.
		cfg.SessionTicketsDisabled = true
		cfg.GetConfigForClient = func(*tls.ClientHelloInfo) (*tls.Config, error) {
			roots, err := loadRoots()
			if err != nil {
				return nil, err
			}
			current := cfg.Clone()
			current.GetConfigForClient = nil
			current.ClientCAs = roots
			return current, nil
		}
	}
	return cfg, nil
}
