package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/cloudwego/eino/callbacks"
	coltrace "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
	"google.golang.org/protobuf/proto"
)

// fakeOpenAI answers Chat Completions like an OpenAI-compatible server: a
// get_weather tool call first, then a text answer once the tool result is in
// the conversation; streamed (SSE, usage in a last chunk) when asked; 404
// model_not_found for "no-such-model".
func fakeOpenAI(t *testing.T) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Model    string `json:"model"`
			Stream   bool   `json:"stream"`
			Messages []struct {
				Role string `json:"role"`
			} `json:"messages"`
		}
		if !strings.HasSuffix(r.URL.Path, "/chat/completions") || json.NewDecoder(r.Body).Decode(&req) != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		if req.Model == "no-such-model" {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusNotFound)
			_, _ = io.WriteString(w, `{"error":{"message":"The model no-such-model does not exist","type":"invalid_request_error","code":"model_not_found"}}`)
			return
		}
		hasTool := false
		for _, m := range req.Messages {
			if m.Role == "tool" {
				hasTool = true
			}
		}
		usage := `{"prompt_tokens":40,"completion_tokens":12,"total_tokens":52}`
		if !req.Stream {
			w.Header().Set("Content-Type", "application/json")
			msg := `{"role":"assistant","content":"Pack a light rain jacket and layers for Kyoto."}`
			finish := "stop"
			if !hasTool {
				msg = `{"role":"assistant","content":"","tool_calls":[{"id":"call_w1","type":"function","function":{"name":"get_weather","arguments":"{\"city\":\"Kyoto\"}"}}]}`
				finish = "tool_calls"
			}
			fmt.Fprintf(w, `{"id":"c1","object":"chat.completion","model":%q,"choices":[{"index":0,"message":%s,"finish_reason":%q}],"usage":%s}`, req.Model, msg, finish, usage)
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		var chunks []string
		if !hasTool {
			chunks = []string{
				`{"choices":[{"index":0,"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call_w1","type":"function","function":{"name":"get_weather","arguments":""}}]}}]}`,
				`{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"city\":\"Kyoto\"}"}}]}}]}`,
				`{"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}`,
			}
		} else {
			chunks = []string{
				`{"choices":[{"index":0,"delta":{"role":"assistant","content":"Pack a light "}}]}`,
				`{"choices":[{"index":0,"delta":{"content":"rain jacket for Kyoto."}}]}`,
				`{"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}`,
			}
		}
		chunks = append(chunks, `{"choices":[],"usage":`+usage+`}`)
		for _, c := range chunks {
			fmt.Fprintf(w, "data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"model\":%q,%s\n\n", req.Model, strings.TrimPrefix(c, "{"))
		}
		_, _ = io.WriteString(w, "data: [DONE]\n\n")
	}))
}

type export struct {
	path, apiKey, secretKey, encoding, contentType string
	body                                           []byte
}

// fakeCollector records every OTLP/HTTP export request.
func fakeCollector(t *testing.T) (*httptest.Server, func() []export) {
	t.Helper()
	var mu sync.Mutex
	var got []export
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		got = append(got, export{r.URL.Path, r.Header.Get("X-Api-Key"), r.Header.Get("X-Secret-Key"),
			r.Header.Get("Content-Encoding"), r.Header.Get("Content-Type"), b})
		mu.Unlock()
		w.Header().Set("Content-Type", "application/x-protobuf")
		w.WriteHeader(http.StatusOK)
	}))
	return srv, func() []export {
		mu.Lock()
		defer mu.Unlock()
		return append([]export(nil), got...)
	}
}

type span struct {
	*tracepb.Span
	attrs map[string]string
}

func decode(t *testing.T, exports []export) ([]span, map[string]string) {
	t.Helper()
	var spans []span
	res := map[string]string{}
	for _, e := range exports {
		var req coltrace.ExportTraceServiceRequest
		if err := proto.Unmarshal(e.body, &req); err != nil {
			t.Fatalf("export body is not OTLP protobuf: %v", err)
		}
		for _, rs := range req.ResourceSpans {
			for k, v := range kvMap(rs.Resource.Attributes) {
				res[k] = v
			}
			for _, ss := range rs.ScopeSpans {
				for _, s := range ss.Spans {
					spans = append(spans, span{s, kvMap(s.Attributes)})
				}
			}
		}
	}
	return spans, res
}

func kvMap(kvs []*commonpb.KeyValue) map[string]string {
	m := map[string]string{}
	for _, kv := range kvs {
		switch v := kv.Value.Value.(type) {
		case *commonpb.AnyValue_StringValue:
			m[kv.Key] = v.StringValue
		case *commonpb.AnyValue_IntValue:
			m[kv.Key] = fmt.Sprint(v.IntValue)
		case *commonpb.AnyValue_BoolValue:
			m[kv.Key] = fmt.Sprint(v.BoolValue)
		default:
			m[kv.Key] = kv.Value.String()
		}
	}
	return m
}

func runExample(t *testing.T, streaming, failing bool, env map[string]string) ([]export, string) {
	t.Helper()
	oa := fakeOpenAI(t)
	defer oa.Close()
	col, exports := fakeCollector(t)
	defer col.Close()
	for k, v := range map[string]string{
		"FI_API_KEY": "fi-api-key-0001", "FI_SECRET_KEY": "fi-secret-key-0002", "FI_PROJECT_NAME": "eino-example-test",
		"FI_BASE_URL": col.URL, "OPENAI_BASE_URL": oa.URL + "/v1", "OPENAI_API_KEY": "sk-openai-key-0003",
		"FI_HIDE_INPUTS": "", "FI_HIDE_OUTPUTS": "",
	} {
		t.Setenv(k, v)
	}
	for k, v := range env {
		t.Setenv(k, v)
	}
	callbacks.InitCallbackHandlers(nil) // run() registers the handler globally
	t.Cleanup(func() { callbacks.InitCallbackHandlers(nil) })
	*stream, *fail, *model = streaming, failing, "gpt-test-1"
	*question = "I am in Kyoto tomorrow, ref QMARK-42. What should I pack?"
	*session = "sess-example"
	if err := run(); err != nil {
		t.Fatalf("run: %v", err)
	}
	return exports(), *question
}

func TestExampleExportsOneTracePerQuestion(t *testing.T) {
	for _, streaming := range []bool{false, true} {
		t.Run(fmt.Sprintf("stream=%v", streaming), func(t *testing.T) {
			exports, _ := runExample(t, streaming, true, nil)
			if len(exports) == 0 {
				t.Fatal("nothing exported")
			}
			for _, e := range exports {
				if e.path != "/tracer/v1/traces" || e.apiKey != "fi-api-key-0001" || e.secretKey != "fi-secret-key-0002" {
					t.Fatalf("export request: path=%q keys set=%v/%v", e.path, e.apiKey != "", e.secretKey != "")
				}
				if e.encoding != "" || e.contentType != "application/x-protobuf" {
					t.Fatalf("export must be uncompressed protobuf: encoding=%q type=%q", e.encoding, e.contentType)
				}
				for _, secret := range []string{"fi-api-key-0001", "fi-secret-key-0002", "sk-openai-key-0003"} {
					if bytes.Contains(e.body, []byte(secret)) {
						t.Fatalf("a key is in the exported spans")
					}
				}
			}
			spans, res := decode(t, exports)
			if res["project_name"] != "eino-example-test" || res["project_type"] != "observe" {
				t.Fatalf("resource = %v", res)
			}
			for k := range res {
				if strings.HasPrefix(k, "host.") || strings.HasPrefix(k, "process.") || strings.HasPrefix(k, "os.") {
					t.Fatalf("resource attribute %s exported", k)
				}
			}
			byTrace := map[string][]span{}
			for _, s := range spans {
				byTrace[fmt.Sprintf("%x", s.TraceId)] = append(byTrace[fmt.Sprintf("%x", s.TraceId)], s)
			}
			if len(byTrace) != 2 {
				t.Fatalf("want two traces (question, failing question), got %d", len(byTrace))
			}
			var ok, failed []span
			for _, ss := range byTrace {
				isErr := false
				for _, s := range ss {
					if s.Status.GetCode() == tracepb.Status_STATUS_CODE_ERROR {
						isErr = true
					}
				}
				if isErr {
					failed = ss
				} else {
					ok = ss
				}
			}
			if ok == nil || failed == nil {
				t.Fatal("want one good and one failed trace")
			}
			kinds := map[string]int{}
			ids := map[string]bool{}
			for _, s := range ok {
				kinds[s.attrs["gen_ai.span.kind"]]++
				ids[fmt.Sprintf("%x", s.SpanId)] = true
				if s.attrs["session.id"] != "sess-example" {
					t.Errorf("%s session.id = %q", s.Name, s.attrs["session.id"])
				}
			}
			roots := 0
			for _, s := range ok {
				if len(s.ParentSpanId) == 0 {
					roots++
					if s.Name != "trip_request" || !strings.Contains(s.attrs["input.value"], "QMARK-42") ||
						!strings.Contains(s.attrs["output.value"], "rain jacket") {
						t.Errorf("root %s: input %q output %q", s.Name, s.attrs["input.value"], s.attrs["output.value"])
					}
				} else if !ids[fmt.Sprintf("%x", s.ParentSpanId)] {
					t.Errorf("%s is orphaned", s.Name)
				}
				switch s.attrs["gen_ai.span.kind"] {
				case "LLM":
					if s.attrs["gen_ai.request.model"] != "gpt-test-1" || s.attrs["gen_ai.usage.total_tokens"] != "52" {
						t.Errorf("LLM span %s: model=%q total=%q", s.Name, s.attrs["gen_ai.request.model"], s.attrs["gen_ai.usage.total_tokens"])
					}
				case "TOOL":
					if s.Name != "get_weather" || !strings.Contains(s.attrs["gen_ai.tool.call.result"], "light rain") {
						t.Errorf("tool span %s result %q", s.Name, s.attrs["gen_ai.tool.call.result"])
					}
				case "AGENT":
					if s.attrs["gen_ai.agent.name"] != "trip_assistant" || !strings.Contains(s.attrs["output.value"], "rain jacket") {
						t.Errorf("agent span: %v", s.attrs)
					}
				}
			}
			if roots != 1 || kinds["AGENT"] != 1 || kinds["LLM"] != 2 || kinds["TOOL"] != 1 {
				t.Fatalf("roots=%d kinds=%v", roots, kinds)
			}
			var llmErr, agentErr bool
			for _, s := range failed {
				if s.Status.GetCode() != tracepb.Status_STATUS_CODE_ERROR {
					continue
				}
				switch s.attrs["gen_ai.span.kind"] {
				case "LLM":
					llmErr = true
					if s.attrs["error.type"] == "" || !strings.Contains(s.attrs["error.message"], "404") {
						t.Errorf("failed model span: error.type=%q error.message=%q", s.attrs["error.type"], s.attrs["error.message"])
					}
					if strings.Contains(s.attrs["error.message"], "127.0.0.1") {
						t.Errorf("error.message carries a host: %q", s.attrs["error.message"])
					}
				case "AGENT":
					agentErr = true
				}
			}
			if !llmErr || !agentErr {
				t.Fatalf("failing run: llm error=%v agent error=%v", llmErr, agentErr)
			}
		})
	}
}

func TestExampleHideSwitchesKeepContentOut(t *testing.T) {
	exports, q := runExample(t, true, true, map[string]string{"FI_HIDE_INPUTS": "true", "FI_HIDE_OUTPUTS": "true"})
	for _, e := range exports {
		for _, s := range []string{"QMARK-42", q, "rain jacket", "light rain", "Kyoto"} {
			if bytes.Contains(e.body, []byte(s)) {
				t.Fatalf("content %q exported with FI_HIDE_INPUTS/FI_HIDE_OUTPUTS on", s)
			}
		}
	}
	spans, _ := decode(t, exports)
	if len(spans) < 8 {
		t.Fatalf("hidden run must still export the spans, got %d", len(spans))
	}
}
