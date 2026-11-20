package auto

import (
	"bytes"
	"context"
	"errors"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
)

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
	insecureWarning = sync.Once{}
	var buf bytes.Buffer
	log.SetOutput(&buf)
	defer log.SetOutput(io.Discard)
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

type probe struct{ calls atomic.Int32 }

func (p *probe) RoundTrip(*http.Request) (*http.Response, error) {
	p.calls.Add(1)
	return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader("")), Header: http.Header{}}, nil
}

// The decision is repeated on the request actually sent, before the transport.
func TestKeyTransportDecidedOnEachRequest(t *testing.T) {
	t.Setenv("FI_ALLOW_INSECURE_HTTP", "")
	for _, target := range []string{"http://collector.internal:4318/tracer/v1/traces", "http://api.futureagi.com/tracer/v1/traces"} {
		p := &probe{}
		tr := &collectorTransport{Transport: http.DefaultTransport.(*http.Transport).Clone(), keyed: p}
		req, _ := http.NewRequest("POST", target, nil)
		req.Header.Set("X-Api-Key", "KEY_VALUE_PRIVATE")
		if _, err := tr.RoundTrip(req); err == nil || p.calls.Load() != 0 {
			t.Fatalf("%s: sent=%d err=%v", target, p.calls.Load(), err)
		} else if strings.Contains(err.Error(), "KEY_VALUE_PRIVATE") || strings.Contains(err.Error(), "collector.internal") {
			t.Fatalf("error not value-free: %v", err)
		}
	}
}

// Rule 5: a proxy named by the environment is refused, nothing reaches it.
func TestKeyTransportRefusesProxy(t *testing.T) {
	var proxied atomic.Int32
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { proxied.Add(1) }))
	defer proxy.Close()
	t.Setenv("FI_ALLOW_INSECURE_HTTP", "1")
	t.Setenv("HTTP_PROXY", proxy.URL)
	t.Setenv("http_proxy", proxy.URL)
	t.Setenv("NO_PROXY", "")
	t.Setenv("no_proxy", "")
	client := exporterClient("", false)
	req, _ := http.NewRequestWithContext(context.Background(), "POST", "http://collector.example.test:4318/tracer/v1/traces", strings.NewReader("x"))
	req.Header.Set("X-Api-Key", "KEY_VALUE_PRIVATE")
	_, err := client.Do(req)
	if !errors.Is(err, errKeyTransport) || proxied.Load() != 0 {
		t.Fatalf("proxied=%d err=%v", proxied.Load(), err)
	}
	if strings.Contains(err.Error(), "KEY_VALUE_PRIVATE") || strings.Contains(err.Error(), proxy.URL) {
		t.Fatalf("error not value-free: %v", err)
	}
}

// Rule 5, https: an HTTPS_PROXY is refused too.
func TestKeyTransportRefusesHTTPSProxy(t *testing.T) {
	var proxied atomic.Int32
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { proxied.Add(1) }))
	defer proxy.Close()
	t.Setenv("HTTPS_PROXY", proxy.URL)
	t.Setenv("https_proxy", proxy.URL)
	t.Setenv("NO_PROXY", "")
	t.Setenv("no_proxy", "")
	req, _ := http.NewRequest("POST", "https://api.futureagi.com/tracer/v1/traces", strings.NewReader("x"))
	if _, err := exporterClient("", false).Do(req); !errors.Is(err, errKeyTransport) || proxied.Load() != 0 {
		t.Fatalf("proxied=%d err=%v", proxied.Load(), err)
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

// Init with a refused target fails closed: nothing is sent, report says failed.
func TestKeyTransportInitRefusedSendsNothing(t *testing.T) {
	p := exporterRegressionSetup(t)
	t.Setenv("FI_ALLOW_INSECURE_HTTP", "")
	t.Setenv("FI_BASE_URL", "http://collector.internal:4318")
	_, err := InitForTest(context.Background(), WithCredentials("DUMMY_FI_API_KEY", "DUMMY_FI_SECRET_KEY"))
	if err == nil {
		t.Fatal("plaintext non-loopback target with keys was accepted")
	}
	if strings.Contains(err.Error(), "collector.internal") || strings.Contains(err.Error(), "DUMMY_FI") {
		t.Fatalf("error not value-free: %v", err)
	}
	_ = p
}
