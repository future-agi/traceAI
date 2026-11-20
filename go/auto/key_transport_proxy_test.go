package auto

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"errors"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"golang.org/x/net/dns/dnsmessage"
)

const proxyTestKey = "KEY_VALUE_PRIVATE"

func clearProxyEnv(t *testing.T) {
	t.Helper()
	for _, k := range []string{"HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy", "FI_ALLOW_INSECURE_HTTP"} {
		t.Setenv(k, "")
	}
}

// recordingProxy is an HTTP proxy that also tunnels CONNECT to a fixed upstream. It records what it saw.
type recordingProxy struct {
	*httptest.Server
	mu       sync.Mutex
	connects []string
	plain    atomic.Int32
	seen     []string
}

func newRecordingProxy(t *testing.T, upstream string) *recordingProxy {
	t.Helper()
	p := &recordingProxy{}
	p.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p.mu.Lock()
		p.seen = append(p.seen, r.Method+" "+r.RequestURI+" "+fmtHeaders(r.Header))
		p.mu.Unlock()
		if r.Method != http.MethodConnect {
			p.plain.Add(1)
			w.WriteHeader(http.StatusBadGateway)
			return
		}
		p.mu.Lock()
		p.connects = append(p.connects, r.Host)
		p.mu.Unlock()
		up, err := net.Dial("tcp", upstream)
		if err != nil {
			w.WriteHeader(http.StatusBadGateway)
			return
		}
		hj := w.(http.Hijacker)
		down, _, err := hj.Hijack()
		if err != nil {
			up.Close()
			return
		}
		_, _ = down.Write([]byte("HTTP/1.1 200 Connection established\r\n\r\n"))
		go func() { _, _ = io.Copy(up, down); up.Close() }()
		go func() { _, _ = io.Copy(down, up); down.Close() }()
	}))
	t.Cleanup(p.Close)
	return p
}

func fmtHeaders(h http.Header) string {
	var b strings.Builder
	for k, v := range h {
		b.WriteString(k + "=" + strings.Join(v, ",") + ";")
	}
	return b.String()
}

func (p *recordingProxy) connectTargets() []string {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]string(nil), p.connects...)
}

// tlsServerFor starts a TLS server whose certificate is issued by a throwaway CA for the given DNS name.
func tlsServerFor(t *testing.T, dnsName string, handler http.Handler) (addr string, roots *x509.CertPool) {
	t.Helper()
	caKey, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	caTemplate := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "test ca"}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
		IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign}
	caDER, err := x509.CreateCertificate(rand.Reader, caTemplate, caTemplate, &caKey.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	ca, _ := x509.ParseCertificate(caDER)
	leafKey, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	leafTemplate := &x509.Certificate{SerialNumber: big.NewInt(2), Subject: pkix.Name{CommonName: dnsName}, DNSNames: []string{dnsName}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}
	leafDER, err := x509.CreateCertificate(rand.Reader, leafTemplate, ca, &leafKey.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewUnstartedServer(handler)
	server.TLS = &tls.Config{Certificates: []tls.Certificate{{Certificate: [][]byte{leafDER}, PrivateKey: leafKey}}}
	server.StartTLS()
	t.Cleanup(server.Close)
	roots = x509.NewCertPool()
	roots.AddCert(ca)
	return server.Listener.Addr().String(), roots
}

// exporterTo returns the production exporter client with the test's trust roots (the host name is still verified
// against them) and, when redirect is set, connections to that host:port are dialed to the local server instead.
func exporterTo(roots *x509.CertPool, redirectFrom, redirectTo string, dialed *[]string, mu *sync.Mutex) *http.Client {
	client := exporterClient("", false)
	tr := client.Transport.(*collectorTransport).Transport
	if roots != nil {
		tr.TLSClientConfig = &tls.Config{RootCAs: roots, MinVersion: tls.VersionTLS12}
	}
	inner := tr.DialContext
	tr.DialContext = func(ctx context.Context, network, addr string) (net.Conn, error) {
		mu.Lock()
		*dialed = append(*dialed, addr)
		mu.Unlock()
		if addr == redirectFrom {
			addr = redirectTo
		}
		return inner(ctx, network, addr)
	}
	return client
}

func post(client *http.Client, target string) (*http.Response, error) {
	req, _ := http.NewRequestWithContext(context.Background(), "POST", target, strings.NewReader("x"))
	req.Header.Set("X-Api-Key", proxyTestKey)
	req.Header.Set("X-Secret-Key", proxyTestKey+"_SECRET")
	return client.Do(req)
}

// HTTPS keeps the environment proxy: the send goes through a CONNECT tunnel, with the host verified against the
// final Future AGI name, and succeeds.
func TestKeyTransportHTTPSThroughProxyUsesFullTLS(t *testing.T) {
	for _, env := range []string{"HTTPS_PROXY", "https_proxy"} {
		t.Run(env, func(t *testing.T) {
			clearProxyEnv(t)
			var got atomic.Value
			addr, roots := tlsServerFor(t, "api.futureagi.com", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				got.Store(r.Header.Get("X-Api-Key") + "|" + r.Header.Get("X-Secret-Key"))
				w.WriteHeader(200)
			}))
			proxy := newRecordingProxy(t, addr)
			t.Setenv(env, proxy.URL)
			var mu sync.Mutex
			var dialed []string
			resp, err := post(exporterTo(roots, "", "", &dialed, &mu), "https://api.futureagi.com/tracer/v1/traces")
			if err != nil {
				t.Fatalf("an https send through the environment proxy was refused or failed: %v", err)
			}
			resp.Body.Close()
			if targets := proxy.connectTargets(); len(targets) != 1 || targets[0] != "api.futureagi.com:443" {
				t.Fatalf("CONNECT targets = %v", targets)
			}
			if got.Load() != proxyTestKey+"|"+proxyTestKey+"_SECRET" {
				t.Fatalf("the keys did not reach the Future AGI host through the tunnel: %v", got.Load())
			}
			proxy.mu.Lock()
			defer proxy.mu.Unlock()
			for _, line := range proxy.seen {
				if strings.Contains(line, proxyTestKey) {
					t.Fatalf("the proxy saw a key: %q", line)
				}
			}
			if strings.Contains(proxy.URL, proxyTestKey) {
				t.Fatal("the proxy URL carries a key")
			}
		})
	}
}

// Through a proxy the certificate is still checked against the written host: a certificate for another name fails
// the handshake and the request never reaches the server.
func TestKeyTransportHTTPSThroughProxyStillVerifiesTheHost(t *testing.T) {
	clearProxyEnv(t)
	var reached atomic.Int32
	addr, roots := tlsServerFor(t, "other.example", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { reached.Add(1) }))
	proxy := newRecordingProxy(t, addr)
	t.Setenv("HTTPS_PROXY", proxy.URL)
	var mu sync.Mutex
	var dialed []string
	resp, err := post(exporterTo(roots, "", "", &dialed, &mu), "https://api.futureagi.com/tracer/v1/traces")
	if err == nil {
		resp.Body.Close()
		t.Fatal("a certificate for another host was accepted through the proxy")
	}
	var unknown x509.HostnameError
	if !errors.As(err, &unknown) {
		t.Fatalf("want a hostname verification failure, got %v", err)
	}
	if reached.Load() != 0 {
		t.Fatal("the request reached a server whose certificate did not match")
	}
	tr := exporterClient("", false).Transport.(*collectorTransport).Transport
	if tr.TLSClientConfig != nil && tr.TLSClientConfig.InsecureSkipVerify {
		t.Fatal("the exporter transport skips certificate verification")
	}
}

// NO_PROXY is honored for https too: the send goes direct.
func TestKeyTransportHTTPSNoProxyGoesDirect(t *testing.T) {
	clearProxyEnv(t)
	addr, roots := tlsServerFor(t, "api.futureagi.com", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	proxy := newRecordingProxy(t, addr)
	t.Setenv("HTTPS_PROXY", proxy.URL)
	t.Setenv("NO_PROXY", "api.futureagi.com")
	var mu sync.Mutex
	var dialed []string
	resp, err := post(exporterTo(roots, "api.futureagi.com:443", addr, &dialed, &mu), "https://api.futureagi.com/tracer/v1/traces")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if len(proxy.connectTargets()) != 0 {
		t.Fatal("NO_PROXY was not honored")
	}
}

// A key-carrying plaintext send is direct: no proxy variable, in either case, carries it. The dialed address is
// the target itself, never the proxy, and the proxy sees nothing.
func TestKeyTransportPlaintextIsNeverProxied(t *testing.T) {
	for _, env := range []string{"HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy"} {
		t.Run(env, func(t *testing.T) {
			clearProxyEnv(t)
			var received atomic.Value
			collector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				received.Store(r.Header.Get("X-Api-Key"))
				w.WriteHeader(200)
			}))
			defer collector.Close()
			proxy := newRecordingProxy(t, collector.Listener.Addr().String())
			t.Setenv(env, proxy.URL)
			t.Setenv("FI_ALLOW_INSECURE_HTTP", "1")
			var mu sync.Mutex
			var dialed []string
			// A named, non-loopback host the proxy variables would normally cover; its connections go to the local collector.
			resp, err := post(exporterTo(nil, "collector.internal:4318", collector.Listener.Addr().String(), &dialed, &mu), "http://collector.internal:4318/tracer/v1/traces")
			if err != nil {
				t.Fatalf("the opted-in plaintext send failed: %v", err)
			}
			resp.Body.Close()
			if received.Load() != proxyTestKey {
				t.Fatalf("the collector did not receive the keys: %v", received.Load())
			}
			mu.Lock()
			defer mu.Unlock()
			if len(dialed) != 1 || dialed[0] != "collector.internal:4318" {
				t.Fatalf("dialed %v: the send did not go straight to its target", dialed)
			}
			if proxy.plain.Load() != 0 || len(proxy.connectTargets()) != 0 {
				t.Fatal("the proxy saw a key-carrying plaintext send")
			}
		})
	}
}

// Loopback plaintext is direct as well.
func TestKeyTransportLoopbackPlaintextIsDirectWithProxyVariablesSet(t *testing.T) {
	clearProxyEnv(t)
	var received atomic.Value
	collector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { received.Store(r.Header.Get("X-Api-Key")) }))
	defer collector.Close()
	proxy := newRecordingProxy(t, collector.Listener.Addr().String())
	for _, env := range []string{"HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"} {
		t.Setenv(env, proxy.URL)
	}
	resp, err := post(exporterClient("", false), collector.URL+"/tracer/v1/traces")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if received.Load() != proxyTestKey || proxy.plain.Load() != 0 {
		t.Fatalf("received=%v proxied=%d", received.Load(), proxy.plain.Load())
	}
}

// The proxy decision itself, read per request: https follows the environment, plaintext is always direct.
func TestKeyTransportProxyDecision(t *testing.T) {
	clearProxyEnv(t)
	t.Setenv("HTTP_PROXY", "http://proxy.internal:3128")
	t.Setenv("HTTPS_PROXY", "http://proxy.internal:3129")
	proxy := exporterClient("", false).Transport.(*collectorTransport).Transport.Proxy
	for target, want := range map[string]string{
		"http://collector.internal:4318/x":  "",
		"https://api.futureagi.com/x":       "http://proxy.internal:3129",
		"https://collector.internal:4318/x": "http://proxy.internal:3129",
	} {
		req, _ := http.NewRequest("POST", target, nil)
		got, err := proxy(req)
		if err != nil {
			t.Fatal(err)
		}
		if (got == nil && want != "") || (got != nil && got.String() != want) {
			t.Fatalf("%s: proxy = %v, want %q", target, got, want)
		}
	}
	t.Setenv("HTTPS_PROXY", "")
	req, _ := http.NewRequest("POST", "https://api.futureagi.com/x", nil)
	if got, _ := proxy(req); got != nil {
		t.Fatalf("a proxy variable removed later is still used: %v", got)
	}
}

// fakeResolver answers every lookup with one fixed IPv4 address.
func fakeResolver(ip [4]byte) *net.Resolver {
	return &net.Resolver{PreferGo: true, Dial: func(ctx context.Context, network, address string) (net.Conn, error) {
		client, server := net.Pipe()
		go func() {
			defer server.Close()
			buf := make([]byte, 4096)
			n, err := server.Read(buf)
			if err != nil {
				return
			}
			var query dnsmessage.Message
			if err := query.Unpack(buf[:n]); err != nil || len(query.Questions) == 0 {
				return
			}
			q := query.Questions[0]
			answer := dnsmessage.Message{Header: dnsmessage.Header{ID: query.ID, Response: true, Authoritative: true, RecursionAvailable: true}, Questions: query.Questions}
			if q.Type == dnsmessage.TypeA {
				answer.Answers = []dnsmessage.Resource{{Header: dnsmessage.ResourceHeader{Name: q.Name, Type: dnsmessage.TypeA, Class: dnsmessage.ClassINET, TTL: 1}, Body: &dnsmessage.AResource{A: ip}}}
			}
			out, err := answer.Pack()
			if err != nil {
				return
			}
			_, _ = server.Write(out)
		}()
		return client, nil
	}}
}

// The loopback guard is part of the exporter transport: "localhost" resolving to a non-loopback address is refused at
// connect time, before any bytes (and so any key) are sent.
func TestKeyTransportDialGuardIsWiredIntoTheExporter(t *testing.T) {
	clearProxyEnv(t)
	previous := dialResolver
	dialResolver = fakeResolver([4]byte{10, 255, 255, 1})
	t.Cleanup(func() { dialResolver = previous })
	started := time.Now()
	resp, err := post(exporterClient("", false), "http://localhost:4318/tracer/v1/traces")
	if err == nil {
		resp.Body.Close()
		t.Fatal("a loopback name that resolves to a non-loopback address was connected")
	}
	if !errors.Is(err, errKeyTransport) {
		t.Fatalf("want the key-transport refusal at connect, got %v", err)
	}
	if time.Since(started) > 4*time.Second {
		t.Fatalf("the refusal waited for a connection attempt: %v", time.Since(started))
	}
	if strings.Contains(err.Error(), proxyTestKey) || strings.Contains(err.Error(), "10.255.255.1") {
		t.Fatalf("error not value-free: %v", err)
	}
}
