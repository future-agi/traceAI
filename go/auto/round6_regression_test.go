package auto

import (
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	collector "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/protobuf/proto"
)

// A valid 172-byte mono PCM WAV: its 232-character base64 encoding is below
// the generic media threshold, but audio must be omitted regardless of size.
func shortWAV() string {
	b := make([]byte, 172)
	copy(b, "RIFF")
	binary.LittleEndian.PutUint32(b[4:], uint32(len(b)-8))
	copy(b[8:], "WAVEfmt ")
	binary.LittleEndian.PutUint32(b[16:], 16)
	binary.LittleEndian.PutUint16(b[20:], 1)
	binary.LittleEndian.PutUint16(b[22:], 1)
	binary.LittleEndian.PutUint32(b[24:], 8000)
	binary.LittleEndian.PutUint32(b[28:], 16000)
	binary.LittleEndian.PutUint16(b[32:], 2)
	binary.LittleEndian.PutUint16(b[34:], 16)
	copy(b[36:], "data")
	binary.LittleEndian.PutUint32(b[40:], uint32(len(b)-44))
	return base64.StdEncoding.EncodeToString(b)
}

func audioOTelValue(v any) attribute.Value {
	switch v := v.(type) {
	case string:
		return attribute.StringValue(v)
	case map[string]any:
		var values []attribute.KeyValue
		for key, value := range v {
			values = append(values, attribute.KeyValue{Key: attribute.Key(key), Value: audioOTelValue(value)})
		}
		return attribute.MapValue(values...)
	case []any:
		var values []attribute.Value
		for _, value := range v {
			values = append(values, audioOTelValue(value))
		}
		return attribute.SliceValue(values...)
	default:
		panic("unsupported test value")
	}
}

func TestNestedAudioSourceOTLPPrivacy(t *testing.T) {
	for _, shape := range []string{"bedrock", "source-array", "mime-nested", "typed-audio"} {
		for _, layer := range []string{"json", "native-map", "wire"} {
			t.Run(shape+"/"+layer, func(t *testing.T) {
				p := exporterRegressionSetup(t)
				payload := shortWAV()
				if len(payload) != 232 {
					t.Fatal("invalid regression fixture size")
				}
				source := map[string]any{"bytes": payload}
				audio := map[string]any{"format": "wav", "transcript": "safe transcript", "source": source}
				var content any = map[string]any{"audio": audio}
				switch shape {
				case "source-array":
					audio["source"] = []any{source}
					content = map[string]any{"input_audio": audio}
				case "mime-nested":
					audio["mimeType"] = "audio/wav"
					content = map[string]any{"inlineData": audio}
				case "typed-audio":
					audio["type"] = "audio"
					content = audio
				}
				body := map[string]any{"messages": []any{map[string]any{"role": "user", "content": []any{content}}}}
				raw, err := json.Marshal(body)
				if err != nil {
					t.Fatal(err)
				}
				value := attribute.StringValue(string(raw))
				if layer == "native-map" {
					value = audioOTelValue(body)
				}
				attrs := []attribute.KeyValue{{Key: "input.value", Value: value}}
				if layer != "wire" {
					p = sdktrace.NewTracerProvider(sdktrace.WithResource(resource.NewSchemaless(attrs...)))
					defer p.Shutdown(context.Background())
				}
				app := tracetest.NewSpanRecorder()
				p.RegisterSpanProcessor(app)
				received := make(chan *collector.ExportTraceServiceRequest, 2)
				sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					data, readErr := io.ReadAll(r.Body)
					request := &collector.ExportTraceServiceRequest{}
					if readErr != nil || proto.Unmarshal(data, request) != nil {
						t.Error("invalid OTLP request")
					}
					received <- request
					w.Header().Set("Content-Type", "application/x-protobuf")
				}))
				defer sink.Close()
				t.Setenv("FI_BASE_URL", sink.URL)
				if _, err := InitForTest(context.Background(), WithTracerProvider(p), WithCredentials("DUMMY_FI_KEY", "DUMMY_FI_SECRET")); err != nil {
					t.Fatal(err)
				}
				if layer == "wire" {
					provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						data, readErr := io.ReadAll(r.Body)
						if readErr != nil || string(data) != string(raw) {
							t.Error("application audio upload changed")
						}
						w.Header().Set("Content-Type", "application/json")
						_, _ = io.WriteString(w, `{"output":{"message":{"role":"assistant","content":[{"text":"hello"}]}}}`)
					}))
					defer provider.Close()
					base := unwrapTransport(http.DefaultTransport).(*wireTransport).base.(*http.Transport).Clone()
					base.Proxy = nil
					base.DialContext = func(ctx context.Context, _, _ string) (net.Conn, error) {
						return (&net.Dialer{}).DialContext(ctx, "tcp", provider.Listener.Addr().String())
					}
					defer base.CloseIdleConnections()
					r, _ := http.NewRequest("POST", "http://bedrock-runtime.us-east-1.amazonaws.com/model/test/converse", strings.NewReader(string(raw)))
					r.Header.Set("Content-Type", "application/json")
					resp, err := (&http.Client{Transport: Transport(base)}).Do(r)
					if err != nil {
						t.Fatal(err)
					}
					_, _ = io.Copy(io.Discard, resp.Body)
					_ = resp.Body.Close()
				} else {
					_, span := p.Tracer("library", trace.WithInstrumentationAttributes(attrs...)).Start(context.Background(), "audio-source", trace.WithAttributes(attrs...), trace.WithLinks(trace.Link{SpanContext: trace.NewSpanContext(trace.SpanContextConfig{TraceID: trace.TraceID{1}, SpanID: trace.SpanID{2}}), Attributes: attrs}))
					span.AddEvent("media", trace.WithAttributes(attrs...))
					span.End()
				}
				if err := ForceFlush(context.Background()); err != nil {
					t.Fatal(err)
				}
				var request *collector.ExportTraceServiceRequest
				select {
				case request = <-received:
				default:
					t.Fatal("collector received no OTLP export")
				}
				data, err := proto.Marshal(request)
				if err != nil {
					t.Fatal(err)
				}
				if strings.Contains(string(data), payload) || strings.Contains(string(data), payload[:32]) {
					t.Fatal("collector-received OTLP contains raw nested WAV payload")
				}
				if !strings.Contains(string(data), "safe transcript") || !strings.Contains(string(data), "wav") {
					t.Fatal("safe audio transcript/metadata lost")
				}
				if len(app.Ended()) != 1 {
					t.Fatal("expected exactly one span")
				}
				if layer != "wire" && !strings.Contains(spanAttr(app.Ended()[0], "input.value").Emit(), payload) {
					t.Fatal("application-owned snapshot mutated")
				}
			})
		}
	}
}

func TestNativeDeadlineAndCancellationParity(t *testing.T) {
	for _, h2 := range []bool{false, true} {
		for _, path := range []string{"/unrelated", "/v1/chat/completions"} {
			for _, stage := range []string{"headers", "body"} {
				for _, source := range []string{"client", "context", "legacy-channel"} {
					name := fmt.Sprintf("h%d%s/%s/%s", map[bool]int{false: 1, true: 2}[h2], path, stage, source)
					t.Run(name, func(t *testing.T) {
						ownedRegressionSetup(t)
						if _, err := InitForTest(context.Background(), WithSpanProcessor(tracetest.NewSpanRecorder())); err != nil {
							t.Fatal(err)
						}
						entered, release := make(chan struct{}, 2), make(chan struct{})
						server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
							_, _ = io.Copy(io.Discard, r.Body)
							if stage == "body" {
								w.Header().Set("Content-Type", "application/json")
								_, _ = io.WriteString(w, `{"choices":[`)
								w.(http.Flusher).Flush()
							}
							entered <- struct{}{}
							select {
							case <-r.Context().Done():
							case <-release:
							}
						}))
						server.EnableHTTP2 = h2
						server.StartTLS()
						defer server.Close()
						defer close(release)
						base := server.Client().Transport.(*http.Transport)
						defer base.CloseIdleConnections()
						type outcome struct {
							text, kind, data            string
							deadline, canceled, timeout bool
							protocol                    int
						}
						run := func(wrapped bool) outcome {
							transport := http.RoundTripper(base)
							if wrapped {
								transport = Transport(base)
							}
							ctx := context.Background()
							timeout := 100 * time.Millisecond
							if source != "client" {
								duration := 100 * time.Millisecond
								if source == "legacy-channel" {
									duration = 2 * time.Second
								}
								var cancel context.CancelFunc
								ctx, cancel = context.WithTimeout(ctx, duration)
								defer cancel()
								timeout = time.Hour
							}
							r, _ := http.NewRequestWithContext(ctx, "POST", server.URL+path, strings.NewReader(round4Request))
							bodyReady := make(chan struct{})
							if source == "legacy-channel" {
								cancel := make(chan struct{})
								r.Cancel = cancel
								go func() {
									<-entered
									if stage == "body" {
										<-bodyReady // Do not race cancellation against delivery of headers.
									}
									close(cancel)
								}()
							}
							resp, err := (&http.Client{Transport: transport, Timeout: timeout}).Do(r)
							result := outcome{}
							if resp != nil {
								result.protocol = resp.ProtoMajor
								if source == "legacy-channel" && stage == "body" {
									prefix := make([]byte, len(`{"choices":[`))
									if _, readErr := io.ReadFull(resp.Body, prefix); readErr != nil {
										t.Fatal(readErr)
									}
									result.data = string(prefix)
								}
								close(bodyReady) // Cancellation now interrupts a stalled read, not buffered data.
								data, readErr := io.ReadAll(resp.Body)
								_ = resp.Body.Close()
								result.data += string(data)
								err = readErr
							} else {
								close(bodyReady)
							}
							if source != "legacy-channel" {
								<-entered
							}
							if err == nil {
								t.Fatal("stalled request did not fail")
							}
							result.text, result.kind = err.Error(), fmt.Sprintf("%T", err)
							result.deadline, result.canceled = errors.Is(err, context.DeadlineExceeded), errors.Is(err, context.Canceled)
							var network net.Error
							result.timeout = errors.As(err, &network) && network.Timeout()
							return result
						}
						baseline, actual := run(false), run(true)
						if !reflect.DeepEqual(actual, baseline) {
							t.Fatalf("native timeout/cancellation changed\nbaseline=%+v\nwrapped=%+v", baseline, actual)
						}
					})
				}
			}
		}
	}
}
