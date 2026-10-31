package traceaiopenai_test

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	traceaiopenai "github.com/future-agi/traceAI/go/openai"
	"github.com/openai/openai-go/v3"
	"github.com/openai/openai-go/v3/option"
	"github.com/openai/openai-go/v3/responses"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

const responseJSON = `{"id":"resp_fixture","object":"response","model":"gpt-4.1-mini","status":"completed","output":[{"type":"message","id":"msg_1","role":"assistant","status":"completed","content":[{"type":"output_text","text":"answer-MARKER-7f3c-do-not-export","annotations":[]}]},{"type":"function_call","id":"fc_1","call_id":"call_1","name":"lookup","arguments":"{\"q\":\"MARKER-7f3c-do-not-export\"}"}],"usage":{"input_tokens":12,"output_tokens":7,"total_tokens":19,"input_tokens_details":{"cached_tokens":3},"output_tokens_details":{"reasoning_tokens":2}}}`
const embeddingJSON = `{"object":"list","model":"text-embedding-3-small","data":[{"object":"embedding","index":0,"embedding":[0.123456789,0.987654321]}],"usage":{"prompt_tokens":2,"total_tokens":2}}`

func extendedClient(t *testing.T, handler http.HandlerFunc, cfg traceaiopenai.TraceConfig) (openai.Client, *tracetest.SpanRecorder, *sdktrace.TracerProvider) {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	rec := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	client := openai.NewClient(option.WithBaseURL(srv.URL+"/v1/"), option.WithAPIKey(testOpenAIKey), option.WithMaxRetries(0),
		option.WithMiddleware(traceaiopenai.Middleware(traceaiopenai.WithTracerProvider(tp), traceaiopenai.WithTraceConfig(cfg))))
	return client, rec, tp
}

func responseParams() responses.ResponseNewParams {
	return responses.ResponseNewParams{Model: "gpt-4.1-mini", Instructions: openai.String("instruction-" + marker), Input: responses.ResponseNewParamsInputUnion{OfString: openai.String("input-" + marker)}, MaxOutputTokens: openai.Int(32)}
}

func TestResponsesJSONAndEmbeddingsCoverage(t *testing.T) {
	for _, hidden := range []bool{false, true} {
		t.Run(fmt.Sprint(hidden), func(t *testing.T) {
			client, rec, tp := extendedClient(t, func(w http.ResponseWriter, r *http.Request) {
				body, _ := io.ReadAll(r.Body)
				if !strings.Contains(string(body), marker) || r.Header.Get("Authorization") != "Bearer "+testOpenAIKey {
					t.Error("middleware changed request content or authentication")
				}
				w.Header().Set("Content-Type", "application/json")
				if strings.HasSuffix(r.URL.Path, "/responses") {
					_, _ = io.WriteString(w, responseJSON)
				} else {
					_, _ = io.WriteString(w, embeddingJSON)
				}
			}, traceaiopenai.TraceConfig{HideInputs: hidden, HideOutputs: hidden})
			ctx, parent := tp.Tracer("caller").Start(context.Background(), "parent")
			ctx = traceaiopenai.ContextWithSessionID(ctx, "session-extended")
			response, err := client.Responses.New(ctx, responseParams())
			if err != nil || response.ID != "resp_fixture" || !strings.Contains(response.OutputText(), marker) {
				t.Fatalf("response changed: %v %v", response, err)
			}
			embedding, err := client.Embeddings.New(ctx, openai.EmbeddingNewParams{Model: "text-embedding-3-small", Input: openai.EmbeddingNewParamsInputUnion{OfString: openai.String(marker)}, EncodingFormat: openai.EmbeddingNewParamsEncodingFormatFloat})
			if err != nil || len(embedding.Data) != 1 || embedding.Data[0].Embedding[0] != 0.123456789 {
				t.Fatalf("embedding changed: %v %v", embedding, err)
			}
			if len(rec.Ended()) != 2 {
				t.Fatalf("got %d spans, want Responses and Embeddings", len(rec.Ended()))
			}
			for i, s := range rec.Ended() {
				a := attrs(s)
				op, kind, model, tokens := "generate_content", "LLM", "gpt-4.1-mini", "12"
				if i == 1 {
					op, kind, model, tokens = "embeddings", "EMBEDDING", "text-embedding-3-small", "2"
				}
				for k, want := range map[string]string{traceaiopenai.GenAIOperationName: op, traceaiopenai.GenAISpanKind: kind, traceaiopenai.GenAIRequestModel: model, traceaiopenai.GenAIResponseModel: model, traceaiopenai.GenAIUsageInputTokens: tokens, traceaiopenai.GenAIProviderName: "openai", traceaiopenai.SessionID: "session-extended"} {
					if str(a, k) != want {
						t.Errorf("%s = %q, want %q", k, str(a, k), want)
					}
				}
				if s.Parent().SpanID() != parent.SpanContext().SpanID() || s.SpanKind() != trace.SpanKindClient {
					t.Error("parent/client span kind lost")
				}
				text := allText(s)
				if strings.Contains(text, testOpenAIKey) || strings.Contains(text, "0.123456789") {
					t.Error("credentials or vectors captured")
				}
				if hidden && strings.Contains(text, marker) {
					t.Errorf("hidden content leaked: %s", text)
				}
				if !hidden && !strings.Contains(str(a, traceaiopenai.InputValue), marker) {
					t.Error("input not captured")
				}
				if i == 0 {
					for k, want := range map[string]string{traceaiopenai.GenAIUsageOutputTokens: "7", traceaiopenai.GenAIUsageTotalTokens: "19", traceaiopenai.GenAIUsageCacheReadTokens: "3", traceaiopenai.GenAIUsageReasoningTokens: "2", traceaiopenai.GenAIRequestMaxTokens: "32"} {
						if str(a, k) != want {
							t.Errorf("%s = %q, want %s", k, str(a, k), want)
						}
					}
					if !hidden && str(a, "gen_ai.output.messages.1.message.tool_calls.0.tool_call.id") != "call_1" {
						t.Error("function call missing")
					}
				} else if _, ok := a[traceaiopenai.GenAIUsageOutputTokens]; ok {
					t.Error("invented embedding output token count")
				}
			}
			parent.End()
		})
	}
}

func writeEvent(w http.ResponseWriter, data string) {
	_, _ = fmt.Fprintf(w, "event: %s\r\ndata: %s\r\n\r\n", "response", data)
	w.(http.Flusher).Flush()
}

func TestResponsesStreamLifecycle(t *testing.T) {
	for _, mode := range []string{"complete", "close", "cancel", "failed", "incomplete", "error"} {
		t.Run(mode, func(t *testing.T) {
			client, rec, _ := extendedClient(t, func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "text/event-stream")
				writeEvent(w, `{"type":"response.output_text.delta","output_index":0,"content_index":0,"delta":"partial"}`)
				if mode == "close" || mode == "cancel" {
					<-r.Context().Done()
					return
				}
				switch mode {
				case "complete":
					writeEvent(w, `{"type":"response.completed","response":`+responseJSON+`}`)
				case "failed":
					writeEvent(w, `{"type":"response.failed","response":{"id":"resp_failed","status":"failed","error":{"code":"server_error","message":"`+marker+`"}}}`)
				case "incomplete":
					writeEvent(w, `{"type":"response.incomplete","response":{"id":"resp_incomplete","status":"incomplete","incomplete_details":{"reason":"max_output_tokens"}}}`)
				case "error":
					writeEvent(w, `{"type":"error","code":"server_error","message":"`+marker+`"}`)
				}
				// A terminal event must end the span even before the HTTP stream closes.
				<-r.Context().Done()
			}, traceaiopenai.TraceConfig{HideInputs: true, HideOutputs: true})
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			stream := client.Responses.NewStreaming(ctx, responseParams())
			defer stream.Close()
			if !stream.Next() {
				t.Fatalf("no first event: %v", stream.Err())
			}
			if stream.Current().Type != "response.output_text.delta" {
				t.Fatal("first event changed")
			}
			if mode == "close" || mode == "cancel" {
				if len(rec.Ended()) != 0 {
					t.Fatal("span ended before stream consumption")
				}
				if mode == "cancel" {
					cancel()
					for stream.Next() {
					}
				}
			} else {
				stream.Next() // SDK reports error events through Err, not Current.
				if len(rec.Ended()) != 1 {
					t.Fatal("terminal event did not end span without EOF")
				}
			}
			_ = stream.Close()
			s := onlySpan(t, rec)
			wantError := mode != "complete" && mode != "close"
			if (s.Status().Code == codes.Error) != wantError {
				t.Errorf("status = %v", s.Status())
			}
			if strings.Contains(allText(s), marker) || strings.Contains(allText(s), "partial") {
				t.Error("stream privacy leaked")
			}
			if mode == "complete" && str(attrs(s), traceaiopenai.GenAIUsageInputTokens) != "12" {
				t.Error("terminal usage missing")
			}
		})
	}
}

func TestResponsesStreamDoesNotDuplicateSnapshots(t *testing.T) {
	client, rec, _ := extendedClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		for _, ev := range []string{
			`{"type":"response.output_item.added","output_index":1,"item":{"type":"function_call","call_id":"call_1","name":"lookup","arguments":""}}`,
			`{"type":"response.function_call_arguments.delta","output_index":1,"delta":"{\"q\":"}`,
			`{"type":"response.output_text.delta","output_index":0,"delta":"answer-"}`,
			`{"type":"response.function_call_arguments.delta","output_index":1,"delta":"\"` + marker + `\"}"}`,
			`{"type":"response.output_text.delta","output_index":0,"delta":"` + marker + `"}`,
			`{"type":"response.completed","response":` + responseJSON + `}`,
		} {
			writeEvent(w, ev)
		}
	}, traceaiopenai.TraceConfig{})
	stream := client.Responses.NewStreaming(context.Background(), responseParams())
	count := 0
	for stream.Next() {
		count++
	}
	if stream.Err() != nil || count != 6 {
		t.Fatalf("SDK stream changed: count=%d, err=%v", count, stream.Err())
	}
	_ = stream.Close()
	a := attrs(onlySpan(t, rec))
	if str(a, "gen_ai.output.messages.0.message.content") != "answer-"+marker {
		t.Error("output duplicated or lost")
	}
	if str(a, "gen_ai.output.messages.1.message.tool_calls.0.tool_call.function.arguments") != `{"q":"`+marker+`"}` {
		t.Error("arguments duplicated or lost")
	}
	var output any
	if json.Unmarshal([]byte(str(a, traceaiopenai.OutputValue)), &output) != nil {
		t.Error("output.value not JSON")
	}
}
