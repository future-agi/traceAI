package auto

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/http/httptrace"
	"net/url"
	"reflect"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/sdk/resource"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	"golang.org/x/net/http2"
	"google.golang.org/protobuf/proto"
)

var s5CredentialSerial atomic.Uint64

// Repeated runs must rediscover credentials, not pass because a previous run
// left the same value in the intentionally process-wide registry.
func s5FreshCredential(label string) string {
	return fmt.Sprintf("S5_%s_PRIVATE_%016d", label, s5CredentialSerial.Add(1))
}

func TestS5PostHookAuthRawTransport(t *testing.T) {
	for _, protocol := range []string{"http1", "http2", "xnet-http2"} {
		for _, composition := range []string{"wrapped-saved-native", "preinstalled-default"} {
			for _, coverage := range []string{"wire", "model-covered", "explicit-suppression", "unmatched-route"} {
				t.Run(protocol+"/"+composition+"/"+coverage, func(t *testing.T) {
					p := exporterRegressionSetup(t)
					app := tracetest.NewSpanRecorder()
					p.RegisterSpanProcessor(app)
					received := privacyCollector(t)
					secret := s5FreshCredential("LATE_AUTH_" + protocol + "_" + composition + "_" + coverage)
					querySecret := s5FreshCredential("LATE_QUERY_" + protocol + "_" + composition + "_" + coverage)
					provider := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						data, _ := io.ReadAll(r.Body)
						if string(data) != round4Request || r.Header.Get("Authorization") != "Bearer "+secret || r.URL.Query().Get("client_secret") != querySecret {
							t.Error("post-hook authenticated request changed")
						}
						if protocol != "http1" && r.ProtoMajor != 2 {
							t.Error("HTTP/2 regression did not use HTTP/2")
						}
						w.Header().Set("Content-Type", "application/json")
						w.Header().Set("Trailer", "X-Final")
						w.WriteHeader(http.StatusUnauthorized)
						_, _ = fmt.Fprintf(w, `{"error":{"message":"rejected %s %s"}}`, secret, querySecret)
						w.Header().Set("X-Final", "unchanged")
					}))
					if protocol == "http1" {
						provider.Start()
					} else {
						provider.EnableHTTP2 = true
						provider.StartTLS()
					}
					defer provider.Close()
					native := provider.Client().Transport.(*http.Transport).Clone()
					defer native.CloseIdleConnections()
					var raw http.RoundTripper = native
					if protocol == "xnet-http2" {
						h2 := &http2.Transport{TLSClientConfig: native.TLSClientConfig.Clone()}
						defer h2.CloseIdleConnections()
						raw = h2
					}
					auth := round4TransportFunc(func(r *http.Request) (*http.Response, error) {
						out := r.Clone(r.Context())
						out.Header.Set("Authorization", "Bearer "+secret)
						out.URL.RawQuery = url.Values{"client_secret": {querySecret}}.Encode()
						return raw.RoundTrip(out)
					})
					type outcome struct {
						status                     int
						body, contentType, trailer string
						readErr, closeErr          error
						headers                    []string
					}
					exchange := func(ctx context.Context, transport http.RoundTripper) outcome {
						t.Helper()
						var mu sync.Mutex
						var headers []string
						ctx = httptrace.WithClientTrace(ctx, &httptrace.ClientTrace{WroteHeaderField: func(k string, vv []string) {
							mu.Lock()
							headers = append(headers, strings.ToLower(k)+"="+strings.Join(vv, ","))
							mu.Unlock()
						}})
						path := "/v1/chat/completions"
						if coverage == "unmatched-route" {
							path = "/ordinary-application-route"
						}
						req, _ := http.NewRequestWithContext(ctx, "POST", provider.URL+path, strings.NewReader(round4Request))
						req.Header.Set("Content-Type", "application/json")
						resp, err := (&http.Client{Transport: transport}).Do(req)
						if err != nil {
							t.Fatal(err)
						}
						data, readErr := io.ReadAll(resp.Body)
						closeErr := resp.Body.Close()
						mu.Lock()
						defer mu.Unlock()
						sort.Strings(headers)
						if req.Header.Get("Authorization") != "" || req.URL.RawQuery != "" {
							t.Fatal("observation modified the original request")
						}
						return outcome{resp.StatusCode, string(data), resp.Header.Get("Content-Type"), resp.Trailer.Get("X-Final"), readErr, closeErr, headers}
					}
					baseline := exchange(context.Background(), auth)
					oldDefault := http.DefaultTransport
					if composition == "preinstalled-default" {
						http.DefaultTransport = auth
						t.Cleanup(func() { http.DefaultTransport = oldDefault })
					}
					if _, err := InitForTest(context.Background(), WithCredentials("S5_FOLLOWUP_FI_KEY", "S5_FOLLOWUP_FI_SECRET")); err != nil {
						t.Fatal(err)
					}
					ctx, parent := p.Tracer("library").Start(context.Background(), "parent")
					if coverage == "model-covered" {
						parent.SetAttributes(attribute.String("gen_ai.span.kind", "LLM"))
					}
					ctx, nativeSpan := p.Tracer("native").Start(ctx, "native")
					_, prestarted := p.Tracer("application").Start(ctx, "prestarted")
					_, unrelated := p.Tracer("application").Start(context.Background(), "unrelated")
					if coverage == "explicit-suppression" {
						ctx = SuppressInstrumentation(ctx)
					}
					transport := http.DefaultTransport
					if composition == "wrapped-saved-native" {
						transport = Transport(auth)
					}
					actual := exchange(ctx, transport)
					if !reflect.DeepEqual(actual, baseline) {
						t.Fatal("agent changed response status/bytes/headers/trailers/errors or application httptrace callbacks")
					}
					for _, span := range []trace.Span{prestarted, unrelated, nativeSpan, parent} {
						span.SetAttributes(attribute.String("output.value", actual.body))
						span.SetStatus(codes.Error, actual.body)
						span.RecordError(errors.New(actual.body))
						span.End()
					}
					data, err := proto.Marshal(flushedPrivacyExport(t, received))
					if err != nil {
						t.Fatal(err)
					}
					for _, value := range []string{secret, querySecret} {
						if processSecrets.snapshot().replace(value) != "<redacted>" || strings.Contains(string(data), value) {
							t.Error("post-hook credential was not discovered or reached OTLP")
						}
					}
					want := 4
					if coverage == "wire" {
						want++
					}
					if len(app.Ended()) != want {
						t.Fatalf("wire dedup/delivery changed: got %d spans, want %d", len(app.Ended()), want)
					}
				})
			}
		}
	}
}

func TestS5AdditionalCredentialHeaders(t *testing.T) {
	for _, header := range []string{"X-Amz-Security-Token", "X-Auth-Token", "Authorization"} {
		t.Run(header, func(t *testing.T) {
			p := exporterRegressionSetup(t)
			received := privacyCollector(t)
			secret := s5FreshCredential("RUNTIME_HEADER_" + header)
			value := secret
			if header == "Authorization" {
				value = "Bearer " + secret
			}
			provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get(header) != value {
					t.Error("runtime credential header changed")
				}
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(401)
				_, _ = fmt.Fprintf(w, `{"error":{"message":"%s"}}`, secret)
			}))
			defer provider.Close()
			if _, err := InitForTest(context.Background(), WithCredentials("S5_HEADER_FI_KEY", "S5_HEADER_FI_SECRET")); err != nil {
				t.Fatal(err)
			}
			req, _ := http.NewRequest("POST", provider.URL+"/v1/chat/completions", strings.NewReader(round4Request))
			req.Header.Set("Content-Type", "application/json")
			// Deliberately noncanonical map keys also occur in custom clients.
			req.Header[strings.ToLower(header)] = []string{value}
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			body, readErr := io.ReadAll(resp.Body)
			closeErr := resp.Body.Close()
			if readErr != nil || closeErr != nil || !strings.Contains(string(body), secret) {
				t.Fatal("application response changed")
			}
			_, span := p.Tracer("application").Start(context.Background(), "unrelated")
			span.SetStatus(codes.Error, string(body))
			span.End()
			data, err := proto.Marshal(flushedPrivacyExport(t, received))
			if err != nil || strings.Contains(string(data), secret) || processSecrets.snapshot().replace(secret) != "<redacted>" {
				t.Fatal("runtime credential header echo escaped registry/export sanitizer")
			}
		})
	}
}

func TestS5IndexedCredentialFieldsOTLP(t *testing.T) {
	for _, key := range []string{"http.request.header.authorization.0", "gen_ai.tool.call.arguments.api_key.0", "http.request.header.authorization", "gen_ai.tool.call.arguments.api_key.0.1"} {
		t.Run(key, func(t *testing.T) {
			p := exporterRegressionSetup(t)
			app := tracetest.NewSpanRecorder()
			p.RegisterSpanProcessor(app)
			received := privacyCollector(t)
			if _, err := InitForTest(context.Background(), WithCredentials("S5_INDEXED_FI_KEY", "S5_INDEXED_FI_SECRET")); err != nil {
				t.Fatal(err)
			}
			secret := s5FreshCredential("UNREGISTERED_STRUCTURAL")
			attr := attribute.String(key, secret)
			if key == "http.request.header.authorization" {
				attr = attribute.StringSlice(key, []string{secret})
			}
			parent := trace.NewSpanContext(trace.SpanContextConfig{TraceID: trace.TraceID{1}, SpanID: trace.SpanID{2}})
			_, span := p.Tracer("indexed", trace.WithInstrumentationAttributes(attr)).Start(context.Background(), "indexed", trace.WithAttributes(attr, attribute.String("safe.0", "safe")), trace.WithLinks(trace.Link{SpanContext: parent, Attributes: []attribute.KeyValue{attr}}))
			span.AddEvent("indexed", trace.WithAttributes(attr))
			span.End()
			data, err := proto.Marshal(flushedPrivacyExport(t, received))
			if err != nil || strings.Contains(string(data), secret) {
				t.Error("indexed credential reached collector through attributes/events/links/scope")
			}
			view := exportSpan(app.Ended()[0], agent.current.config, resource.NewSchemaless(attr))
			for _, kv := range view.Resource().Attributes() {
				if strings.Contains(kv.Value.Emit(), secret) {
					t.Error("indexed resource credential escaped sanitizer")
				}
			}
			if !strings.Contains(spanAttr(app.Ended()[0], key).Emit(), secret) {
				t.Fatal("structural redaction mutated application snapshot")
			}
		})
	}
}
