package traceaibedrock

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime/types"
	"github.com/aws/smithy-go"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
)

// Exercise the real SDK's request serializer, signer and response/event-stream
// decoders on both sides of the capture limit. Size is a content-capture policy,
// not a reason to drop the entire semantic span for a supported Messages call.
func TestLargeInvokeRequestWire(t *testing.T) {
	for _, streaming := range []bool{false, true} {
		for _, size := range []int{2*maxCapture - 1, 2 * maxCapture, 2*maxCapture + 1, 70324, 1 << 20} {
			for name, privacy := range map[string]TraceConfig{
				"visible":           {},
				"hidden":            {HideInputs: true, HideOutputs: true},
				"hidden-messages":   {HideInputMessages: true, HideOutputMessages: true},
				"hidden-text":       {HideInputText: true, HideOutputText: true},
				"hidden-parameters": {HideLLMInvocationParameters: true},
			} {
				t.Run(fmt.Sprintf("stream=%t/bytes=%d/%s", streaming, size, name), func(t *testing.T) {
					body := largeInvokeRequest(size)
					p, e := provider(t)
					raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
						got, err := io.ReadAll(r.Body)
						if err != nil || string(got) != body || len(got) != size {
							t.Error("request body changed", err)
						}
						if !strings.HasPrefix(r.Header.Get("Authorization"), "AWS4-HMAC-SHA256") {
							t.Error("SDK signing missing")
						}
						writeLargeInvokeResponse(t, w, streaming, true)
					})
					c := NewClient(raw, WithTracerProvider(p), WithTraceConfig(privacy))
					ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
					defer cancel()
					if err := callLargeInvoke(t, ctx, c, streaming, model, body, true); err != nil {
						t.Fatal(err)
					}
					spans := llms(e)
					if len(spans) != 1 {
						t.Fatalf("supported %d-byte request succeeded but emitted %d semantic spans; want 1", size, len(spans))
					}
					s := spans[0]
					a := attrs(s)
					op := "InvokeModel"
					if streaming {
						op = "InvokeModelWithResponseStream"
					}
					if s.Name != "bedrock."+op || a["rpc.method"].AsString() != op || a["gen_ai.request.model"].AsString() != model || a["gen_ai.provider.name"].AsString() != "aws.bedrock" {
						t.Error("semantic operation/model metadata missing")
					}
					if s.Status.Code == codes.Error || !s.EndTime.After(s.StartTime) || a["traceai.stream.closed_early"].AsBool() {
						t.Error("successful call did not complete normally")
					}
					for key, want := range map[string]int64{"input_tokens": 0, "output_tokens": 2, "cache_read_tokens": 3, "cache_write_tokens": 4, "cache_write_5m_tokens": 1, "cache_write_1h_tokens": 3} {
						assertCacheCounter(t, a, key, &want)
					}
					assertCacheCounter(t, a, "total_tokens", nil)
					if got := a["gen_ai.response.finish_reasons"].AsStringSlice(); len(got) != 1 || got[0] != "end_turn" {
						t.Error("finish reason lost")
					}
					for key, want := range map[string]attribute.Value{
						"gen_ai.request.max_tokens":  attribute.Int64Value(100),
						"gen_ai.request.temperature": attribute.Float64Value(0),
						"gen_ai.request.top_p":       attribute.Float64Value(0.5),
					} {
						got, present := a[key]
						if privacy.HideLLMInvocationParameters {
							if present {
								t.Errorf("hidden parameter %s captured", key)
							}
						} else if !present || got != want {
							t.Errorf("%s lost: got %v want %v", key, got, want)
						}
					}
					if privacy.input() && !a["traceai.capture.truncated"].AsBool() {
						t.Error("input capture limit not marked")
					}
					if size > 2*maxCapture || !privacy.input() {
						for key := range a {
							if strings.HasPrefix(key, "gen_ai.input.messages") || strings.HasPrefix(key, "gen_ai.tool.definitions") {
								t.Errorf("uncaptured input attribute %s", key)
							}
						}
						if !privacy.HideInputs {
							if _, present := a["input.value"]; present {
								t.Error("fabricated input content")
							}
						}
					}
					if privacy.HideInputs && a["input.value"].AsString() != "__REDACTED__" {
						t.Error("input redaction marker missing")
					}
					if privacy.HideOutputs && a["output.value"].AsString() != "__REDACTED__" {
						t.Error("output redaction marker missing")
					}
					if privacy.output() && !strings.Contains(a["output.value"].AsString(), "large-request-answer") {
						t.Error("small completion lost with large input")
					}
					if streaming && a["gen_ai.server.time_to_first_token"].Type() != attribute.FLOAT64 {
						t.Error("stream timing lost")
					}
					encoded, err := json.Marshal(spans)
					if err != nil {
						t.Fatal(err)
					}
					for _, secret := range []string{"binary-canary", "thinking-canary", "signature-canary"} {
						if strings.Contains(string(encoded), secret) {
							t.Errorf("unsupported content leaked: %s", secret)
						}
					}
					if size > 2*maxCapture || !privacy.input() {
						for _, secret := range []string{"input-canary", "system-canary", "tool-canary"} {
							if strings.Contains(string(encoded), secret) {
								t.Errorf("uncaptured input leaked: %s", secret)
							}
						}
					}
					if !privacy.output() && strings.Contains(string(encoded), "large-request-answer") {
						t.Error("hidden output leaked")
					}
				})
			}
		}
	}
}

func largeInvokeRequest(size int) string {
	body := strings.Replace(invokeBody, `"temperature":0`, `"temperature":0,"top_p":0.5`, 1)
	return strings.Replace(body, "input-canary", "input-canary"+strings.Repeat("x", size-len(body)), 1)
}

const largeInvokeUsage = `{"input_tokens":0,"output_tokens":2,"cache_read_input_tokens":3,"cache_creation_input_tokens":4,"cache_creation":{"ephemeral_5m_input_tokens":1,"ephemeral_1h_input_tokens":3}}`
const largeInvokeResult = `{"content":[{"type":"text","text":"large-request-answer"}],"stop_reason":"end_turn","usage":` + largeInvokeUsage + `}`

func writeLargeInvokeResponse(t *testing.T, w http.ResponseWriter, streaming, complete bool) {
	t.Helper()
	if !streaming {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, largeInvokeResult)
		return
	}
	w.Header().Set("Content-Type", "application/vnd.amazon.eventstream")
	payloads := []string{
		`{"type":"message_start","message":{"usage":` + largeInvokeUsage + `}}`,
		`{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"large-request-answer"}}`,
		`{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}`,
	}
	if complete {
		payloads = append(payloads, `{"type":"message_stop"}`)
	}
	for _, payload := range payloads {
		data, err := json.Marshal(map[string]string{"bytes": base64.StdEncoding.EncodeToString([]byte(payload))})
		if err != nil {
			t.Fatal(err)
		}
		writeCacheFrame(t, w, "chunk", string(data))
	}
}

func callLargeInvoke(t *testing.T, ctx context.Context, c *Client, streaming bool, modelID, body string, complete bool) error {
	t.Helper()
	if !streaming {
		out, err := c.InvokeModel(ctx, &bedrockruntime.InvokeModelInput{ModelId: aws.String(modelID), Body: []byte(body), ContentType: aws.String("application/json")})
		if err == nil && (out == nil || string(out.Body) != largeInvokeResult) {
			t.Error("SDK response changed")
		}
		return err
	}
	out, err := c.InvokeModelWithResponseStream(ctx, &bedrockruntime.InvokeModelWithResponseStreamInput{ModelId: aws.String(modelID), Body: []byte(body), ContentType: aws.String("application/json")})
	if err != nil {
		return err
	}
	s := out.GetStream()
	defer s.Close()
	count := 0
	for event := range s.Events() {
		v, ok := event.(*types.ResponseStreamMemberChunk)
		if !ok || len(v.Value.Bytes) == 0 {
			t.Error("SDK event changed")
		}
		count++
	}
	want := 3
	if complete {
		want = 4
	}
	if count != want {
		t.Errorf("native stream events = %d, want %d", count, want)
	}
	return s.Err()
}

func TestLargeInvokeRequestErrors(t *testing.T) {
	for _, mode := range []string{"sync-http-error", "stream-http-error", "stream-incomplete"} {
		t.Run(mode, func(t *testing.T) {
			p, e := provider(t)
			raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
				if mode == "stream-incomplete" {
					writeLargeInvokeResponse(t, w, true, false)
					return
				}
				w.Header().Set("Content-Type", "application/json")
				w.Header().Set("X-Amzn-Errortype", "ValidationException")
				w.WriteHeader(http.StatusBadRequest)
				_, _ = io.WriteString(w, `{"message":"private-service-error-canary"}`)
			})
			c := NewClient(raw, WithTracerProvider(p), WithTraceConfig(TraceConfig{}))
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			err := callLargeInvoke(t, ctx, c, mode != "sync-http-error", model, largeInvokeRequest(2*maxCapture+1), false)
			wantKind := "request_error"
			if mode == "stream-incomplete" {
				wantKind = "incomplete_stream"
				// A clean native EOF stays nil; only telemetry marks the missing
				// semantic terminal event as an incomplete stream.
				if err != nil {
					t.Errorf("native EOF changed: %v", err)
				}
			} else {
				var apiErr smithy.APIError
				if !errors.As(err, &apiErr) || apiErr.ErrorCode() != "ValidationException" || apiErr.ErrorMessage() != "private-service-error-canary" {
					t.Errorf("SDK error changed: %v", err)
				}
			}
			spans := llms(e)
			if len(spans) != 1 {
				t.Fatalf("large failing request emitted %d semantic spans, want 1", len(spans))
			}
			a := attrs(spans[0])
			if spans[0].Status.Code != codes.Error || a["error.type"].AsString() != wantKind {
				t.Errorf("failure telemetry lost: %v", spans[0].Status)
			}
			encoded, _ := json.Marshal(spans)
			if strings.Contains(string(encoded), "private-service-error-canary") || strings.Contains(string(encoded), "input-canary") {
				t.Error("uncaptured/error content leaked")
			}
		})
	}
}
