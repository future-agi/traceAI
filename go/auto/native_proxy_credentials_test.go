package auto

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/protobuf/proto"
)

// Both saved aliases of the default native transport and explicitly registered
// native transports must observe HTTP/1.1 targets before any response exists.
func TestS5NativeHTTP1LateQueryWithoutResponse(t *testing.T) {
	for _, installation := range []string{"saved-default-native", "explicit-native"} {
		for _, coverage := range []string{"wire", "model-covered", "explicit-suppression", "unmatched-route"} {
			t.Run(installation+"/"+coverage, func(t *testing.T) {
				p := exporterRegressionSetup(t)
				received := privacyCollector(t)
				secret := s5FreshCredential("HTTP1_TARGET")
				provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if r.ProtoMajor != 1 || r.URL.Query().Get("client_secret") != secret {
						t.Error("native HTTP/1.1 request target changed")
					}
					_, _ = io.Copy(io.Discard, r.Body)
					panic(http.ErrAbortHandler)
				}))
				defer provider.Close()
				native := &http.Transport{}
				defer native.CloseIdleConnections()
				auth := round4TransportFunc(func(r *http.Request) (*http.Response, error) {
					out := r.Clone(r.Context())
					out.URL.RawQuery = url.Values{"client_secret": {secret}}.Encode()
					resp, err := native.RoundTrip(out)
					if err != nil {
						err = fmt.Errorf("auth rejected %s: %w", secret, err)
					}
					return resp, err
				})
				exchange := func(ctx context.Context, transport http.RoundTripper) error {
					t.Helper()
					path := "/v1/chat/completions"
					if coverage == "unmatched-route" {
						path = "/ordinary"
					}
					req, _ := http.NewRequestWithContext(ctx, "POST", provider.URL+path, strings.NewReader(round4Request))
					req.Header.Set("Content-Type", "application/json")
					resp, err := transport.RoundTrip(req)
					if resp != nil || err == nil || req.URL.RawQuery != "" {
						t.Fatal("unexpected response/error or modified original URL")
					}
					return err
				}
				baseline := exchange(context.Background(), auth)
				if installation == "saved-default-native" {
					old := http.DefaultTransport
					http.DefaultTransport = native
					t.Cleanup(func() { http.DefaultTransport = old })
				}
				if _, err := InitForTest(context.Background(), WithCredentials("S5_NATIVE_FI_KEY", "S5_NATIVE_FI_SECRET")); err != nil {
					t.Fatal(err)
				}
				if installation == "explicit-native" {
					_ = Transport(native) // Install once before handing it to opaque auth code.
					_ = Transport(native) // Idempotent, without adding callback layers.
				}
				ctx, parent := p.Tracer("library").Start(context.Background(), "parent")
				if coverage == "model-covered" {
					parent.SetAttributes(attribute.String("gen_ai.span.kind", "LLM"))
				}
				_, child := p.Tracer("application").Start(ctx, "prestarted")
				_, unrelated := p.Tracer("application").Start(context.Background(), "unrelated")
				if coverage == "explicit-suppression" {
					ctx = SuppressInstrumentation(ctx)
				}
				actual := exchange(ctx, Transport(auth))
				if actual.Error() != baseline.Error() || !errors.Is(actual, io.EOF) || !errors.Is(baseline, io.EOF) {
					t.Fatal("agent changed the native transport error")
				}
				if processSecrets.snapshot().replace(secret) != "<redacted>" {
					t.Fatal("native HTTP/1.1 target credential was not observed")
				}
				for _, span := range []trace.Span{child, unrelated, parent} {
					span.SetAttributes(attribute.String("output.value", secret))
					span.SetStatus(codes.Error, actual.Error())
					span.RecordError(actual)
					span.End()
				}
				data, err := proto.Marshal(flushedPrivacyExport(t, received))
				if err != nil || strings.Contains(string(data), secret) {
					t.Fatal("native HTTP/1.1 target credential reached OTLP")
				}
			})
		}
	}
}

func TestS5NativeProxyHookErrorAndPanicParity(t *testing.T) {
	for _, hook := range []string{"proxy", "connect-headers"} {
		for _, failure := range []string{"error", "panic"} {
			t.Run(hook+"/"+failure, func(t *testing.T) {
				ownedRegressionSetup(t)
				proxyURL, _ := url.Parse("http://127.0.0.1:1")
				sentinel := errors.New("original callback failure")
				var calls atomic.Int64
				callback := func() error {
					calls.Add(1)
					if failure == "panic" {
						panic(sentinel)
					}
					return sentinel
				}
				native := &http.Transport{}
				defer native.CloseIdleConnections()
				if hook == "proxy" {
					native.Proxy = func(*http.Request) (*url.URL, error) { return nil, callback() }
				} else {
					native.Proxy = http.ProxyURL(proxyURL)
					native.GetProxyConnectHeader = func(context.Context, *url.URL, string) (http.Header, error) {
						return nil, callback()
					}
				}
				// Invoke callbacks directly: native CONNECT header callbacks run
				// on a dial goroutine, where an application panic terminates the
				// process. Our callback wrapper must not swallow that panic.
				exchange := func() (err error, panicked any) {
					defer func() { panicked = recover() }()
					if hook == "proxy" {
						req, _ := http.NewRequest("POST", "https://127.0.0.1:1/ordinary", nil)
						_, err = native.Proxy(req)
					} else {
						_, err = native.GetProxyConnectHeader(context.Background(), proxyURL, "127.0.0.1:1")
					}
					return
				}
				baselineErr, baselinePanic := exchange()
				if _, err := InitForTest(context.Background(), WithSpanProcessor(tracetest.NewSpanRecorder())); err != nil {
					t.Fatal(err)
				}
				_ = Transport(native)
				actualErr, actualPanic := exchange()
				if baselinePanic != actualPanic || !errors.Is(baselineErr, sentinel) && baselinePanic != sentinel || !errors.Is(actualErr, sentinel) && actualPanic != sentinel || calls.Load() != 2 {
					t.Fatal("native callback error, panic identity or call count changed")
				}
			})
		}
	}
}

func TestS5NativeCloneRetainsStaticCONNECTConfiguration(t *testing.T) {
	ownedRegressionSetup(t)
	if _, err := InitForTest(context.Background(), WithSpanProcessor(tracetest.NewSpanRecorder())); err != nil {
		t.Fatal(err)
	}
	native := &http.Transport{ProxyConnectHeader: http.Header{"Proxy-Authorization": {"Bearer original-static"}}}
	_ = Transport(native)
	clone := native.Clone()
	defer clone.CloseIdleConnections()
	clone.ProxyConnectHeader.Set("Proxy-Authorization", "Bearer replacement-static")
	_ = Transport(clone)
	if native.GetProxyConnectHeader != nil || clone.GetProxyConnectHeader != nil || native.ProxyConnectHeader.Get("Proxy-Authorization") != "Bearer original-static" || clone.ProxyConnectHeader.Get("Proxy-Authorization") != "Bearer replacement-static" {
		t.Fatal("observer changed the native static header/Clone configuration semantics")
	}
	req, _ := http.NewRequest("GET", "https://127.0.0.1:1", nil)
	_, _ = clone.Proxy(req) // Only exercise configuration: no network operation.
	if processSecrets.snapshot().replace("replacement-static") != "<redacted>" {
		t.Fatal("cloned native transport did not observe its own static headers")
	}
}

func TestS5NativeProxyCONNECTCredentials(t *testing.T) {
	for _, source := range []string{"static", "dynamic", "userinfo"} {
		for _, coverage := range []string{"wire", "model-covered", "explicit-suppression", "unmatched-route"} {
			t.Run(source+"/"+coverage, func(t *testing.T) {
				p := exporterRegressionSetup(t)
				app := tracetest.NewSpanRecorder()
				p.RegisterSpanProcessor(app)
				received := privacyCollector(t)
				secret := s5FreshCredential("CONNECT_" + source)
				password := secret
				value := "Bearer " + secret
				if source == "userinfo" {
					r, _ := http.NewRequest("GET", "http://127.0.0.1", nil)
					r.SetBasicAuth("proxy-user", secret)
					value = r.Header.Get("Authorization")
					// The value actually transmitted is the Basic token, not the
					// password's pre-encoding representation (ROUND5 B2 scope).
					secret = strings.TrimPrefix(value, "Basic ")
				}
				var count atomic.Int64
				proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if r.Method != "CONNECT" || r.Header.Get("Proxy-Authorization") != value {
						t.Error("native CONNECT authentication changed")
					}
					count.Add(1)
					conn, _, err := w.(http.Hijacker).Hijack()
					if err != nil {
						t.Error(err)
						return
					}
					defer conn.Close()
					_, _ = fmt.Fprintf(conn, "HTTP/1.1 407 rejected %s\r\nContent-Length: 0\r\n\r\n", secret)
				}))
				defer proxy.Close()
				proxyURL, _ := url.Parse(proxy.URL)
				if source == "userinfo" {
					proxyURL.User = url.UserPassword("proxy-user", password)
				}
				headers := http.Header{"Proxy-Authorization": {value}}
				var proxyCalls, headerCalls, responseCalls atomic.Int64
				native := &http.Transport{Proxy: func(req *http.Request) (*url.URL, error) {
					proxyCalls.Add(1)
					return proxyURL, nil
				}, OnProxyConnectResponse: func(ctx context.Context, proxyURL *url.URL, req *http.Request, resp *http.Response) error {
					responseCalls.Add(1)
					if req.Header.Get("Proxy-Authorization") != value || !strings.Contains(resp.Status, secret) {
						t.Error("application CONNECT response callback changed")
					}
					return nil
				}}
				defer native.CloseIdleConnections()
				if source == "static" {
					native.ProxyConnectHeader = headers
				} else if source == "dynamic" {
					native.GetProxyConnectHeader = func(ctx context.Context, u *url.URL, target string) (http.Header, error) {
						headerCalls.Add(1)
						if u != proxyURL || target != "127.0.0.1:1" {
							t.Error("dynamic CONNECT callback arguments changed")
						}
						return headers, nil
					}
				}
				exchange := func(ctx context.Context, transport http.RoundTripper) error {
					t.Helper()
					path := "/v1/chat/completions"
					if coverage == "unmatched-route" {
						path = "/ordinary"
					}
					req, _ := http.NewRequestWithContext(ctx, "POST", "https://127.0.0.1:1"+path, strings.NewReader(round4Request))
					req.Header.Set("Content-Type", "application/json")
					resp, err := transport.RoundTrip(req)
					if resp != nil || err == nil || !strings.Contains(err.Error(), secret) {
						t.Fatal("expected unchanged CONNECT auth error")
					}
					return err
				}
				baseline := exchange(context.Background(), native)
				if _, err := InitForTest(context.Background(), WithCredentials("S5_PROXY_FI_KEY", "S5_PROXY_FI_SECRET")); err != nil {
					t.Fatal(err)
				}
				ctx, parent := p.Tracer("application").Start(context.Background(), "parent")
				if coverage == "model-covered" {
					parent.SetAttributes(attribute.String("gen_ai.span.kind", "LLM"))
				}
				_, prestarted := p.Tracer("application").Start(ctx, "prestarted")
				_, unrelated := p.Tracer("application").Start(context.Background(), "unrelated")
				if coverage == "explicit-suppression" {
					ctx = SuppressInstrumentation(ctx)
				}
				actual := exchange(ctx, Transport(native))
				if actual.Error() != baseline.Error() || count.Load() != 2 || proxyCalls.Load() != 2 || responseCalls.Load() != 2 || source == "dynamic" && headerCalls.Load() != 2 {
					t.Fatal("agent changed error/response or native callback counts")
				}
				if !reflect.DeepEqual(headers, http.Header{"Proxy-Authorization": {value}}) || processSecrets.snapshot().replace(secret) != "<redacted>" {
					t.Fatal("CONNECT headers modified or credential not observed")
				}
				for _, span := range []trace.Span{prestarted, unrelated, parent} {
					span.SetAttributes(attribute.String("output.value", secret))
					span.SetStatus(codes.Error, actual.Error())
					span.RecordError(actual)
					span.End()
				}
				data, err := proto.Marshal(flushedPrivacyExport(t, received))
				if err != nil || strings.Contains(string(data), secret) {
					t.Fatal("CONNECT credential escaped to OTLP")
				}
				if !strings.Contains(app.Ended()[len(app.Ended())-1].Status().Description, secret) {
					t.Fatal("agent mutated the application exporter snapshot")
				}
			})
		}
	}
}
