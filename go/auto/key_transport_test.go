package auto

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
)

// resetInsecureWarning makes the next opted-in plaintext decision warn again and returns the buffer the
// warning goes to; the previous once-state and log destination come back when the test ends.
func resetInsecureWarning(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	previous := log.Writer()
	log.SetOutput(&buf)
	insecureWarning = sync.Once{}
	t.Cleanup(func() { log.SetOutput(previous); insecureWarning = sync.Once{} })
	return &buf
}

func decide(t *testing.T, base, optIn string) error {
	t.Helper()
	t.Setenv("FI_ALLOW_INSECURE_HTTP", optIn)
	for _, k := range []string{"HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"} {
		t.Setenv(k, "")
	}
	_, err := collectorEndpoint(Config{BaseURL: base})
	return err
}

// Rules 1-4: every cell of the transport matrix, decided on the host as written.
func TestKeyTransportMatrix(t *testing.T) {
	cases := []struct {
		name, base, optIn string
		ok                bool
	}{
		{"loopback ipv4 plaintext", "http://127.0.0.1:4318", "", true},
		{"loopback localhost plaintext", "http://localhost:4318", "", true},
		{"loopback ipv6 plaintext", "http://[::1]:4318", "", true},
		{"loopback https", "https://127.0.0.1:4318", "", true},
		{"FI https", "https://api.futureagi.com", "", true},
		{"FI subdomain https", "https://eu.api.futureagi.com", "", true},
		{"FI plaintext", "http://api.futureagi.com", "", false},
		{"FI plaintext with opt-in", "http://api.futureagi.com", "1", false},
		{"FI plaintext trailing dot", "http://api.futureagi.com.", "1", false},
		{"FI plaintext upper case", "http://API.FutureAGI.com", "1", false},
		{"FI bare domain plaintext", "http://futureagi.com", "1", false},
		{"lookalike host https", "https://futureagi.com.example.org", "", true},
		{"other plaintext refused", "http://collector.internal:4318", "", false},
		{"other plaintext opt-in 1", "http://collector.internal:4318", "1", true},
		{"opt-in true is not 1", "http://collector.internal:4318", "true", false},
		{"opt-in yes is not 1", "http://collector.internal:4318", "yes", false},
		{"opt-in spaced is not 1", "http://collector.internal:4318", " 1", false},
		{"other https", "https://collector.internal", "", true},
		{"loopback trailing dot is not literal", "http://127.0.0.1.:4318", "", false},
		{"short loopback is not literal", "http://127.1:4318", "", false},
		{"hex loopback is not literal", "http://0x7f.0.0.1:4318", "", false},
		{"decimal loopback is not literal", "http://2130706433:4318", "", false},
		{"short loopback with opt-in", "http://127.1:4318", "1", true},
		{"decimal loopback with opt-in", "http://2130706433:4318", "1", true},
		{"backslash authority", `http://127.0.0.1\@collector.internal:4318`, "1", false},
		{"backslash authority https", `https://api.futureagi.com\@collector.internal`, "", false},
		{"userinfo", "http://u:p@127.0.0.1:4318", "", false},
		{"backslash before at, https FI", `https://\@api.futureagi.com`, "", false},
		{"percent-encoded backslash before at", "http://127.0.0.1%5C@collector.internal:4318", "1", false},
		{"percent-encoded backslash before at, FI", "https://api.futureagi.com%5C@collector.internal", "", false},
		{"at after the authority is a path, not a host: plaintext still needs the opt-in", "http://collector.internal:4318/x@api.futureagi.com", "", false},
		{"at after the authority is a path, not a host: opt-in allows the written host only", "http://collector.internal:4318/x@api.futureagi.com", "1", true},
		{"at after the authority on a loopback host is still loopback", "http://127.0.0.1:4318/x@collector.internal", "", true},
		{"empty authority", "http:///collector.internal:4318", "1", false},
		{"empty authority https FI path", "https:///api.futureagi.com/", "", false},
		{"backslash for authority https FI", `https://\api.futureagi.com/`, "", false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			err := decide(t, c.base, c.optIn)
			if c.ok != (err == nil) {
				t.Fatalf("base %q optIn %q: ok=%v err=%v", c.base, c.optIn, c.ok, err)
			}
			if err != nil && strings.Contains(err.Error(), "collector.internal") {
				t.Fatalf("refusal echoes the configured value: %v", err)
			}
		})
	}
}

// Rule 4: one value-free warning per process for an opted-in plaintext send.
func TestKeyTransportOptInWarnsOnceValueFree(t *testing.T) {
	buf := resetInsecureWarning(t)
	for i := 0; i < 3; i++ {
		if err := decide(t, "http://secret-host.internal:4318", "1"); err != nil {
			t.Fatal(err)
		}
	}
	if n := strings.Count(buf.String(), "FI_ALLOW_INSECURE_HTTP"); n != 1 {
		t.Fatalf("want one warning, got %d: %q", n, buf.String())
	}
	if strings.Contains(buf.String(), "secret-host") {
		t.Fatalf("warning echoes the host: %q", buf.String())
	}
}

// Rule 6: a 3xx on a key-carrying send is an explicit refusal, not a response.
func TestKeyTransportRedirectIsRefusal(t *testing.T) {
	var sink atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { sink.Add(1) }))
	defer target.Close()
	for _, code := range []int{301, 302, 303, 307, 308} {
		collector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			http.Redirect(w, r, target.URL+"/sink", code)
		}))
		req, _ := http.NewRequest("POST", collector.URL+"/tracer/v1/traces", strings.NewReader("x"))
		req.Header.Set("X-Api-Key", "KEY_VALUE_PRIVATE")
		resp, err := exporterClient("", false).Do(req)
		collector.Close()
		if err == nil {
			resp.Body.Close()
			t.Fatalf("%d: redirect delivered as a response", code)
		}
		if strings.Contains(err.Error(), "KEY_VALUE_PRIVATE") || strings.Contains(err.Error(), target.URL) {
			t.Fatalf("%d: error not value-free: %v", code, err)
		}
	}
	if sink.Load() != 0 {
		t.Fatal("redirect target contacted")
	}
}

// The connected peer must agree with a loopback claim: a written loopback
// name that resolves elsewhere is refused at dial time.
func TestKeyTransportDialMustBeLoopback(t *testing.T) {
	if err := checkDialAddress("localhost", "10.1.2.3:4318"); err == nil {
		t.Fatal("non-loopback peer accepted for a loopback claim")
	}
	if err := checkDialAddress("localhost", "127.0.0.1:4318"); err != nil {
		t.Fatal(err)
	}
	if err := checkDialAddress("localhost", "[::1]:4318"); err != nil {
		t.Fatal(err)
	}
	if err := checkDialAddress("collector.internal", "10.1.2.3:4318"); err != nil {
		t.Fatal(err)
	}
}

// The decision is repeated on the request actually sent, before any connection is opened.
func TestKeyTransportDecidedOnEachRequest(t *testing.T) {
	t.Setenv("FI_ALLOW_INSECURE_HTTP", "")
	for _, target := range []string{"http://collector.internal:4318/tracer/v1/traces", "http://api.futureagi.com/tracer/v1/traces"} {
		var dials atomic.Int32
		tr := exporterClient("", false).Transport.(*collectorTransport)
		tr.Transport.DialContext = func(context.Context, string, string) (net.Conn, error) {
			dials.Add(1)
			return nil, errors.New("must not dial")
		}
		req, _ := http.NewRequest("POST", target, nil)
		req.Header.Set("X-Api-Key", "KEY_VALUE_PRIVATE")
		if _, err := tr.RoundTrip(req); !errors.Is(err, errKeyTransport) || dials.Load() != 0 {
			t.Fatalf("%s: dials=%d err=%v", target, dials.Load(), err)
		} else if strings.Contains(err.Error(), "KEY_VALUE_PRIVATE") || strings.Contains(err.Error(), "collector.internal") {
			t.Fatalf("error not value-free: %v", err)
		}
	}
}

type closeTracker struct {
	io.Reader
	closed atomic.Bool
}

func (c *closeTracker) Close() error { c.closed.Store(true); return nil }

// A request the transport refuses still has its body closed (the RoundTripper contract).
func TestKeyTransportRefusedRequestClosesItsBody(t *testing.T) {
	t.Setenv("FI_ALLOW_INSECURE_HTTP", "")
	body := &closeTracker{Reader: strings.NewReader("x")}
	req, _ := http.NewRequest("POST", "http://collector.internal:4318/tracer/v1/traces", body)
	if _, err := exporterClient("", false).Transport.RoundTrip(req); !errors.Is(err, errKeyTransport) {
		t.Fatalf("err=%v", err)
	}
	if !body.closed.Load() {
		t.Fatal("refused request left its body open")
	}
}

// Init with a refused target fails closed: nothing is sent, the report says failed, and nothing echoes a value.
func TestKeyTransportInitRefusedSendsNothing(t *testing.T) {
	p := exporterRegressionSetup(t)
	var reached atomic.Int32
	collector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { reached.Add(1) }))
	defer collector.Close()
	// A plaintext, non-loopback spelling of the collector's own address: the host is not literal loopback.
	base := strings.Replace(collector.URL, "127.0.0.1", "127.1", 1)
	t.Setenv("FI_ALLOW_INSECURE_HTTP", "")
	t.Setenv("FI_BASE_URL", base)
	_, err := InitForTest(context.Background(), WithCredentials("DUMMY_FI_API_KEY", "DUMMY_FI_SECRET_KEY"))
	if !errors.Is(err, errKeyTransport) {
		t.Fatalf("plaintext non-loopback target with keys was accepted: %v", err)
	}
	if strings.Contains(err.Error(), "127.1") || strings.Contains(err.Error(), "DUMMY_FI") {
		t.Fatalf("error not value-free: %v", err)
	}
	exportRegressionSpan(t, p)
	_ = ForceFlush(context.Background())
	if reached.Load() != 0 {
		t.Fatalf("a refused target received %d requests", reached.Load())
	}
	report, _ := json.Marshal(Report())
	var wire string
	for _, integration := range Report().Integrations {
		if integration.Name == "wire" {
			wire = integration.Status
		}
	}
	if wire != "failed" {
		t.Fatalf("report wire status = %q, want failed", wire)
	}
	if strings.Contains(string(report), "DUMMY_FI_API_KEY") || strings.Contains(string(report), "DUMMY_FI_SECRET_KEY") {
		t.Fatalf("report carries a key: %s", report)
	}
}
