package auto_test

import (
	"bytes"
	"compress/flate"
	"compress/gzip"
	"compress/zlib"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"net/textproto"
	"reflect"
	"strings"
	"testing"

	"github.com/future-agi/traceAI/go/auto"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
)

func TestNativeContentPrivacy(t *testing.T) {
	for _, config := range []auto.TraceConfig{{}, {HideInputs: true}, {HideInputText: true}, {HideInputMessages: true}} {
		rec, _ := regressionSetup(t, config)
		for _, tc := range []struct{ endpoint, body string }{
			{"https://codestral.mistral.ai/v1/fim/completions", `{"model":"codestral","prompt":"prefix","suffix":"PRIVATE_NATIVE_CONTENT"}`},
			{"https://bedrock-runtime.us-east-1.amazonaws.com/model/m/converse", `{"messages":[],"toolConfig":{"tools":[{"toolSpec":{"name":"tool","description":"PRIVATE_NATIVE_CONTENT","inputSchema":{"json":{"type":"object"}}}}]}}`},
		} {
			regressionExchange(t, regressionTransport(`{}`, "application/json", 200), context.Background(), tc.endpoint, tc.body, nil)
			s := wires(rec)[len(wires(rec))-1]
			if config == (auto.TraceConfig{}) {
				if got := value(s, "gen_ai.request.parameters"); got == nil || !strings.Contains(got.(string), "PRIVATE_NATIVE_CONTENT") {
					t.Fatalf("native parameters missing when enabled: %v", got)
				}
				continue
			}
			for _, kv := range s.Attributes() {
				if strings.Contains(kv.Value.Emit(), "PRIVATE_NATIVE_CONTENT") {
					t.Fatalf("native content leaked in %s with %+v", kv.Key, config)
				}
			}
		}
	}
}

func TestContextMessagePrivacy(t *testing.T) {
	for _, tc := range []struct {
		name   string
		config auto.TraceConfig
		key    string
	}{
		{"input-text", auto.TraceConfig{HideInputText: true}, "gen_ai.input.messages.0.message.contents.0.message_content.text"},
		{"output-text", auto.TraceConfig{HideOutputText: true}, "gen_ai.output.messages.0.message.contents.0.message_content.text"},
		{"image", auto.TraceConfig{HideInputImages: true}, "gen_ai.input.messages.0.message.contents.0.message_content.image"},
		{"image-url", auto.TraceConfig{HideInputImages: true}, "gen_ai.input.messages.0.message.contents.0.message_content.image.url"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rec, _ := regressionSetup(t, tc.config)
			ctx := auto.ContextWithAttributes(context.Background(), attribute.StringSlice(tc.key, []string{"PRIVATE_CONTEXT_CONTENT"}))
			regressionExchange(t, regressionTransport(responseJSON, "application/json", 200), ctx, "https://api.openai.com/v1/chat/completions", requestJSON, nil)
			for _, kv := range wires(rec)[0].Attributes() {
				if strings.Contains(kv.Value.Emit(), "PRIVATE_CONTEXT_CONTENT") {
					t.Fatalf("context content leaked in %s", kv.Key)
				}
			}
		})
	}
}

func TestPartialToolCredential(t *testing.T) {
	for _, mode := range []string{"failure", "close"} {
		t.Run(mode, func(t *testing.T) {
			rec, _ := regressionSetup(t, auto.TraceConfig{})
			event, _ := json.Marshal(map[string]any{"choices": []any{map[string]any{"index": 0, "delta": map[string]any{"tool_calls": []any{map[string]any{"index": 0, "function": map[string]any{"name": "tool", "arguments": `{"api_key":"PRIVATE_TOOL_CREDENTIAL",`}}}}}}})
			body := &scriptedBody{first: "data: " + string(event) + "\n\n", readErr: context.DeadlineExceeded}
			tr := auto.Transport(roundTripFunc(func(r *http.Request) (*http.Response, error) {
				_, _ = io.Copy(io.Discard, r.Body)
				return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"text/event-stream"}}, Body: body}, nil
			}))
			r, _ := http.NewRequest("POST", "https://api.openai.com/v1/chat/completions", strings.NewReader(requestJSON))
			r.Header.Set("Content-Type", "application/json")
			resp, err := tr.RoundTrip(r)
			if err != nil {
				t.Fatal(err)
			}
			buf := make([]byte, 4096)
			_, _ = resp.Body.Read(buf) // legal zero-length progress, not EOF
			_, _ = resp.Body.Read(buf)
			if mode == "failure" {
				_, _ = resp.Body.Read(buf)
			}
			_ = resp.Body.Close()
			ss := wires(rec)
			if len(ss) != 1 {
				t.Fatalf("spans=%d", len(ss))
			}
			for _, k := range []string{"gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.arguments", "output.value"} {
				got, _ := value(ss[0], k).(string)
				if got == "" || strings.Contains(got, "PRIVATE_TOOL_CREDENTIAL") || !strings.Contains(got, "redacted") {
					t.Fatalf("partial credential leaked or output lost: %s=%s", k, got)
				}
			}
			if (ss[0].Status().Code == codes.Error) != (mode == "failure") {
				t.Fatal("wrong partial-stream status")
			}
		})
	}
}

func TestEncodedMediaPrivacy(t *testing.T) {
	rec, _ := regressionSetup(t, auto.TraceConfig{})
	payload := strings.Repeat("QUJD%2F", 100)
	body := `{"messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"data:image/png;base64,` + payload + `"}}]}]}`
	regressionExchange(t, regressionTransport(responseJSON, "application/json", 200), context.Background(), "https://api.openai.com/v1/chat/completions", body, nil)
	got := value(wires(rec)[0], "input.value").(string)
	if strings.Contains(got, "%2F") || strings.Contains(got, "QUJD") || !strings.Contains(got, "<omitted: 500 base64 chars>") {
		t.Fatalf("encoded media leaked: %s", got)
	}
}

func TestNoBodyFraming(t *testing.T) {
	rec, _ := regressionSetup(t, auto.TraceConfig{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if len(r.TransferEncoding) != 0 || r.ContentLength != 0 {
			w.WriteHeader(http.StatusLengthRequired)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()
	base := &http.Transport{}
	defer base.CloseIdleConnections()
	for _, transport := range []http.RoundTripper{base, auto.Transport(base)} {
		r, _ := http.NewRequest("POST", server.URL+"/v1/chat/completions", strings.NewReader(""))
		if r.Body != http.NoBody {
			t.Fatal("test must exercise net/http's NoBody sentinel")
		}
		resp, err := (&http.Client{Transport: transport}).Do(r)
		if err != nil {
			t.Fatal(err)
		}
		_ = resp.Body.Close()
		if resp.StatusCode != 200 {
			t.Fatalf("empty request framing changed: status=%d", resp.StatusCode)
		}
	}
	// Known-provider empty JSON bodies must also fail validation, rather than
	// being mistaken for a streaming/unavailable body.
	tr := auto.Transport(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.Body != http.NoBody {
			t.Fatal("NoBody identity changed")
		}
		return &http.Response{StatusCode: 200, Header: http.Header{}, Body: http.NoBody}, nil
	}))
	r, _ := http.NewRequest("POST", "https://api.openai.com/v1/chat/completions", strings.NewReader(""))
	resp, err := tr.RoundTrip(r)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if len(wires(rec)) != 0 {
		t.Fatal("empty body passed AI validation")
	}
}

func TestNoBodyResponseFinalizesImmediately(t *testing.T) {
	rec, _ := regressionSetup(t, auto.TraceConfig{})
	tr := auto.Transport(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		_, _ = io.Copy(io.Discard, r.Body)
		return &http.Response{StatusCode: 204, Header: http.Header{}, Body: http.NoBody}, nil
	}))
	r, _ := http.NewRequest("POST", "https://api.openai.com/v1/chat/completions", strings.NewReader(requestJSON))
	r.Header.Set("Content-Type", "application/json")
	resp, err := tr.RoundTrip(r)
	if err != nil {
		t.Fatal(err)
	}
	if resp.Body != http.NoBody || len(wires(rec)) != 1 || value(wires(rec)[0], "traceai.wire.incomplete") != nil {
		t.Fatal("known-empty response not finalized or sentinel changed")
	}
	_ = resp.Body.Close()
	if len(wires(rec)) != 1 {
		t.Fatal("empty response finalized twice")
	}
}

func TestCompressedMultipart(t *testing.T) {
	for _, encoding := range []string{"gzip", "deflate", "raw-deflate", "br"} {
		for _, host := range []string{"api.openai.com", "proxy.example"} {
			t.Run(encoding+"/"+host, func(t *testing.T) {
				rec, _ := regressionSetup(t, auto.TraceConfig{})
				config := auto.Report().Config
				config.WireMaxBodyBytes = 1024
				_ = auto.Shutdown(context.Background()) // Separate first-init configuration scenario.
				if _, err := auto.InitForTest(context.Background(), auto.WithConfig(config), auto.WithSpanProcessor(rec)); err != nil {
					t.Fatal(err)
				}
				var plain bytes.Buffer
				w := multipart.NewWriter(&plain)
				h := textproto.MIMEHeader{"Content-Disposition": {`form-data; name="file"; filename="call.wav"`}, "Content-Type": {"audio/wav"}}
				file, _ := w.CreatePart(h)
				_, _ = file.Write(make([]byte, 256*1024))
				_ = w.WriteField("model", "whisper-1") // after the large file
				_ = w.WriteField("language", "en")
				_ = w.Close()
				var encoded bytes.Buffer
				var compressor io.WriteCloser
				switch encoding {
				case "gzip":
					compressor = gzip.NewWriter(&encoded)
				case "deflate":
					compressor = zlib.NewWriter(&encoded)
				case "raw-deflate":
					compressor, _ = flate.NewWriter(&encoded, flate.DefaultCompression)
				case "br":
					encoded.WriteString("unsupported compressed body")
				}
				if compressor != nil {
					_, _ = compressor.Write(plain.Bytes())
					_ = compressor.Close()
				}
				tr := auto.Transport(roundTripFunc(func(r *http.Request) (*http.Response, error) {
					got, err := io.ReadAll(r.Body)
					_ = r.Body.Close()
					if err != nil || !bytes.Equal(got, encoded.Bytes()) || r.Header.Get("Content-Encoding") == "" {
						t.Fatal("application upload bytes/headers changed")
					}
					return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}}, Body: io.NopCloser(strings.NewReader(`{"text":"hello"}`))}, nil
				}))
				r, _ := http.NewRequest("POST", "https://"+host+"/v1/audio/transcriptions", bytes.NewReader(encoded.Bytes()))
				r.Header.Set("Content-Type", w.FormDataContentType())
				wireEncoding := encoding
				if encoding == "raw-deflate" {
					wireEncoding = "deflate"
				}
				r.Header.Set("Content-Encoding", wireEncoding)
				resp, err := tr.RoundTrip(r)
				if err != nil {
					t.Fatal(err)
				}
				_, _ = io.Copy(io.Discard, resp.Body)
				_ = resp.Body.Close()
				ss := wires(rec)
				if encoding == "br" {
					if host == "proxy.example" {
						if len(ss) != 0 {
							t.Fatal("unknown unavailable multipart captured")
						}
					} else if len(ss) != 1 || value(ss[0], "traceai.wire.undecoded_encoding") != "br" || value(ss[0], "input.value") != nil {
						t.Fatal("unsupported multipart encoding not marked")
					}
					return
				}
				if len(ss) != 1 || value(ss[0], "gen_ai.request.model") != "whisper-1" || value(ss[0], "traceai.audio.input_bytes") != int64(256*1024) || value(ss[0], "gen_ai.audio.mime_type") != "audio/wav" || value(ss[0], "traceai.wire.truncated") != nil || value(ss[0], "output.value") != "hello" {
					t.Fatalf("compressed multipart metadata lost: %v", ss)
				}
			})
		}
	}
}

func TestAttributeBudget(t *testing.T) {
	for _, contextSize := range []int{0, 150} {
		t.Run(fmt.Sprint(contextSize), func(t *testing.T) {
			rec, _ := regressionSetup(t, auto.TraceConfig{})
			ctx := context.Background()
			for i := 0; i < contextSize; i++ {
				ctx = auto.ContextWithAttributes(ctx, attribute.String(fmt.Sprintf("metadata.field%d", i), "value"))
			}
			messages := make([]map[string]string, 100)
			for i := range messages {
				messages[i] = map[string]string{"role": "user", "content": fmt.Sprintf("message %d", i)}
			}
			body, _ := json.Marshal(map[string]any{"model": "m", "messages": messages})
			var previous []string
			for i := 0; i < 4; i++ {
				regressionExchange(t, regressionTransport(responseJSON, "application/json", 200), ctx, "https://api.openai.com/v1/chat/completions", string(body), nil)
				s := wires(rec)[i]
				for _, k := range []string{"gen_ai.span.kind", "gen_ai.provider.name", "gen_ai.operation.name", "gen_ai.request.model", "gen_ai.response.model", "gen_ai.usage.total_tokens", "output.value", "traceai.capture.layer", "traceai.wire.endpoint", "server.address", "http.response.status_code"} {
					if value(s, k) == nil {
						t.Fatalf("mandatory %s lost", k)
					}
				}
				// A7 forbids aggregate input from restoring omitted old messages.
				if value(s, "input.value") != nil || value(s, "traceai.capture.dropped_content_count") == nil {
					t.Fatal("projection restored evicted conversation or omitted its count")
				}
				if len(s.Attributes()) > 128 || s.DroppedAttributes() != 0 || value(s, "traceai.wire.attributes_truncated") != true {
					t.Fatalf("uncontrolled attribute budget: retained=%d dropped=%d", len(s.Attributes()), s.DroppedAttributes())
				}
				var keys []string
				for _, kv := range s.Attributes() {
					keys = append(keys, string(kv.Key))
				}
				if i > 0 && !reflect.DeepEqual(keys, previous) {
					t.Fatal("attribute retention is nondeterministic")
				}
				previous = keys
			}
		})
	}
}
