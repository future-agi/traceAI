package auto

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/protobuf/proto"
)

// Response.Request observation alone is not sufficient: a native transport can
// transmit auth headers, then fail without returning any response at all.
func TestS5PostHookAuthErrorWithoutResponse(t *testing.T) {
	for _, coverage := range []string{"wire", "model-covered", "explicit-suppression", "unmatched-route"} {
		t.Run(coverage, func(t *testing.T) {
			p := exporterRegressionSetup(t)
			received := privacyCollector(t)
			secret := s5FreshCredential("NO_RESPONSE_AUTH_" + coverage)
			provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "Bearer "+secret {
					t.Error("native transport did not transmit auth")
				}
				_, _ = io.Copy(io.Discard, r.Body)
				panic(http.ErrAbortHandler) // HTTP/1.1 closes without response headers.
			}))
			defer provider.Close()
			raw := http.DefaultTransport.(*http.Transport).Clone()
			defer raw.CloseIdleConnections()
			auth := round4TransportFunc(func(r *http.Request) (*http.Response, error) {
				out := r.Clone(r.Context())
				out.Header.Set("Authorization", "Bearer "+secret)
				resp, err := raw.RoundTrip(out)
				if err != nil {
					err = fmt.Errorf("auth request rejected %s: %w", secret, err)
				}
				return resp, err
			})
			exchange := func(ctx context.Context, transport http.RoundTripper) error {
				t.Helper()
				path := "/v1/chat/completions"
				if coverage == "unmatched-route" {
					path = "/unmatched"
				}
				req, _ := http.NewRequestWithContext(ctx, "POST", provider.URL+path, strings.NewReader(round4Request))
				req.Header.Set("Content-Type", "application/json")
				resp, err := transport.RoundTrip(req)
				if resp != nil || err == nil {
					t.Fatal("expected native transport failure with no response")
				}
				return err
			}
			baseline := exchange(context.Background(), auth)
			if _, err := InitForTest(context.Background(), WithCredentials("S5_FAILURE_FI_KEY", "S5_FAILURE_FI_SECRET")); err != nil {
				t.Fatal(err)
			}
			ctx, parent := p.Tracer("application").Start(context.Background(), "parent")
			if coverage == "model-covered" {
				parent.SetAttributes(attribute.String("gen_ai.span.kind", "LLM"))
			}
			if coverage == "explicit-suppression" {
				ctx = SuppressInstrumentation(ctx)
			}
			actual := exchange(ctx, Transport(auth))
			if actual.Error() != baseline.Error() || !errors.Is(actual, io.EOF) || !errors.Is(baseline, io.EOF) {
				t.Fatal("auth/native error changed with agent enabled")
			}
			if processSecrets.snapshot().replace(secret) != "<redacted>" {
				t.Fatal("header trace did not discover auth before transport error")
			}
			parent.SetStatus(codes.Error, actual.Error())
			parent.RecordError(actual)
			parent.End()
			_, unrelated := p.Tracer("application").Start(context.Background(), "unrelated", trace.WithAttributes(attribute.String("output.value", actual.Error())))
			unrelated.End()
			data, err := proto.Marshal(flushedPrivacyExport(t, received))
			if err != nil || strings.Contains(string(data), secret) {
				t.Fatal("post-hook auth escaped OTLP without a response")
			}
		})
	}
}

type s5LegacyResponseTransport struct {
	seen, canceled *http.Request
	response       *http.Response
}

func (t *s5LegacyResponseTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	t.seen = r
	t.response.Request = r
	return t.response, nil
}
func (t *s5LegacyResponseTransport) CancelRequest(r *http.Request) { t.canceled = r }

func TestS5ObservationOnlyCancellationAndBodyParity(t *testing.T) {
	for _, coverage := range []string{"unmatched-route", "explicit-suppression"} {
		t.Run(coverage, func(t *testing.T) {
			ownedRegressionSetup(t)
			if _, err := InitForTest(context.Background(), WithSpanProcessor(tracetest.NewSpanRecorder())); err != nil {
				t.Fatal(err)
			}
			ctx, path := context.Background(), "/unmatched"
			if coverage == "explicit-suppression" {
				ctx, path = SuppressInstrumentation(ctx), "/v1/chat/completions"
			}
			req, _ := http.NewRequestWithContext(ctx, "POST", "http://127.0.0.1"+path, strings.NewReader(round4Request))
			failure := errors.New("original close failure")
			body := &parityBody{reader: strings.NewReader("unchanged"), closeErr: failure, trailer: make(http.Header)}
			original := &http.Response{StatusCode: 200, Body: body}
			base := &s5LegacyResponseTransport{response: original}
			transport := Transport(base)
			resp, err := transport.RoundTrip(req)
			if err != nil || resp != original || resp.Request != req || body.reads != 0 || body.closes != 0 {
				t.Fatal("observation changed response identity or consumed body")
			}
			transport.(interface{ CancelRequest(*http.Request) }).CancelRequest(req)
			if base.canceled != base.seen {
				t.Fatal("observation lost legacy cancellation after response headers")
			}
			data, readErr := io.ReadAll(resp.Body)
			if string(data) != "unchanged" || readErr != nil || resp.Body.Close() != failure || resp.Body.Close() != failure || body.closes != 2 {
				t.Fatal("observation changed response reads or repeated Close errors")
			}
			hook := unwrapTransport(transport).(*wireTransport)
			hook.mu.Lock()
			remaining := len(hook.requests)
			hook.mu.Unlock()
			if remaining != 0 {
				t.Fatal("completed observation retained cancellation mapping")
			}
		})
	}
}
