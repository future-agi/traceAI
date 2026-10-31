package auto

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
)

// Round 6 pass A (2026-10-31): stream observation through the real transport
// and the actual OTLP export. Every case first records an uninstrumented
// baseline and then requires byte/error parity with the agent on.

type r6aExchange struct {
	data   string
	err    string
	status int
}

func r6aDo(t *testing.T, transport http.RoundTripper, url, body string, read func(io.Reader) ([]byte, error)) r6aExchange {
	t.Helper()
	req, _ := http.NewRequest("POST", url, strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	resp, err := (&http.Client{Transport: transport}).Do(req)
	if err != nil {
		t.Fatal(err)
	}
	if read == nil {
		read = io.ReadAll
	}
	data, readErr := read(resp.Body)
	_ = resp.Body.Close()
	out := r6aExchange{data: string(data), status: resp.StatusCode}
	if readErr != nil {
		out.err = fmt.Sprintf("%T:%v", readErr, readErr)
	}
	return out
}

func r6aWireSpan(t *testing.T, request interface {
	GetResourceSpans() []*tracepb.ResourceSpans
}) *tracepb.Span {
	t.Helper()
	var found *tracepb.Span
	for _, rs := range request.GetResourceSpans() {
		for _, ss := range rs.ScopeSpans {
			for _, span := range ss.Spans {
				for _, kv := range span.Attributes {
					if kv.Key == "traceai.capture.layer" && kv.Value.GetStringValue() == "wire" {
						if found != nil {
							t.Fatal("duplicate wire spans")
						}
						found = span
					}
				}
			}
		}
	}
	if found == nil {
		t.Fatal("no wire span exported")
	}
	return found
}

func r6aAttrs(span *tracepb.Span) map[string]*commonpb.AnyValue {
	values := map[string]*commonpb.AnyValue{}
	for _, kv := range span.Attributes {
		values[kv.Key] = kv.Value
	}
	return values
}

// r6aDifferential sends the same request without and with the agent and
// returns the exported wire span (the agent run must match the baseline).
// Catalog hosts (api.cohere.com, port 11434) are dialed to the loopback
// server only; nothing leaves the machine.
func r6aDifferential(t *testing.T, handler http.HandlerFunc, url, body string, read func(io.Reader) ([]byte, error)) (*tracepb.Span, r6aExchange) {
	t.Helper()
	exporterRegressionSetup(t)
	received := privacyCollector(t)
	provider := httptest.NewServer(handler)
	defer provider.Close()
	native := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "tcp", provider.Listener.Addr().String())
	}}
	defer native.CloseIdleConnections()
	baseline := r6aDo(t, native, url, body, read)
	if _, err := InitForTest(context.Background(), WithCredentials("R6A_STREAM_FI_KEY", "R6A_STREAM_FI_SECRET")); err != nil {
		t.Fatal(err)
	}
	actual := r6aDo(t, Transport(native), url, body, read)
	if actual != baseline {
		t.Fatalf("agent changed application behaviour: %+v != %+v", actual, baseline)
	}
	return r6aWireSpan(t, flushedPrivacyExport(t, received)), actual
}

// go-s6 final #2: an NDJSON record only exists after its newline. A response
// failing before the terminal record's newline exports neither its usage nor
// its output; complete earlier records still count.
func TestR6aNDJSONFrameBoundaryOTLP(t *testing.T) {
	const (
		first  = `{"model":"m","message":{"role":"assistant","content":"Hel"},"done":false}` + "\n"
		usage  = `{"model":"m","message":{"role":"assistant","content":""},"done":true,"prompt_eval_count":4,"eval_count":2}` + "\n"
		late   = `{"model":"m","message":{"role":"assistant","content":"LATE_UNTERMINATED"},"done":true,"prompt_eval_count":10,"eval_count":4}`
		chatIn = `{"model":"m","messages":[{"role":"user","content":"hi"}]}`
	)
	for _, tc := range []struct {
		name, body, output string
		total              int64 // -1: absent
		failure            bool
	}{
		{"failure-before-terminal-newline", first + late, "Hel", -1, true},
		{"newline-delivered-control", first + late + "\n", "HelLATE_UNTERMINATED", 14, true},
		{"complete-prefix-usage", first + usage + late, "Hel", 6, true},
		{"clean-eof-unterminated-final-line", first + usage + late, "Hel", 6, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			span, app := r6aDifferential(t, func(w http.ResponseWriter, r *http.Request) {
				_, _ = io.Copy(io.Discard, r.Body)
				w.Header().Set("Content-Type", "application/x-ndjson")
				if tc.failure {
					w.Header().Set("Content-Length", fmt.Sprint(len(tc.body)+16)) // Real unexpected EOF.
				}
				_, _ = io.WriteString(w, tc.body)
			}, "http://localhost:11434/api/chat", chatIn, nil)
			if app.data != tc.body || (app.err != "") != tc.failure {
				t.Fatalf("fixture did not deliver the intended bytes/outcome: %+v", app)
			}
			attrs := r6aAttrs(span)
			if attrs["gen_ai.request.stream"].GetBoolValue() != true {
				t.Fatal("NDJSON stream provenance lost")
			}
			if got := attrs["output.value"].GetStringValue(); got != tc.output {
				t.Fatalf("output.value=%q want %q", got, tc.output)
			}
			total, ok := attrs["gen_ai.usage.total_tokens"]
			if tc.total < 0 && ok || tc.total >= 0 && (!ok || total.GetIntValue() != tc.total) {
				t.Fatalf("usage from unobserved/partial record: %v", total)
			}
			if failed := span.Status.Code == tracepb.Status_STATUS_CODE_ERROR; failed != tc.failure {
				t.Fatalf("outcome changed: %v", span.Status)
			}
		})
	}
}

// go-s6 final #2 (empty-stream provenance): a stream whose transport delivers
// zero body bytes before failing is still an NDJSON/SSE stream (goldens 207,
// 218, 229), without inventing either milestone.
func TestR6aEmptyStreamProvenanceOTLP(t *testing.T) {
	for _, ct := range []string{"application/x-ndjson", "text/event-stream"} {
		t.Run(ct, func(t *testing.T) {
			span, app := r6aDifferential(t, func(w http.ResponseWriter, r *http.Request) {
				_, _ = io.Copy(io.Discard, r.Body)
				w.Header().Set("Content-Type", ct)
				w.Header().Set("Content-Length", "64")
				w.WriteHeader(http.StatusOK)
			}, "http://localhost:11434/api/chat", `{"model":"m","messages":[{"role":"user","content":"hi"}]}`, nil)
			if app.data != "" || app.err == "" {
				t.Fatalf("expected zero bytes and a transport failure: %+v", app)
			}
			attrs := r6aAttrs(span)
			if attrs["gen_ai.request.stream"].GetBoolValue() != true {
				t.Fatal("zero-byte stream lost stream provenance")
			}
			for _, key := range []string{"gen_ai.response.time_to_first_chunk", "gen_ai.response.time_to_first_text"} {
				if _, ok := attrs[key]; ok {
					t.Fatalf("invented %s for zero delivered bytes", key)
				}
			}
		})
	}
}

// go-s6 final #3: a standard SSE `event:` line names the Cohere v2 delta when
// the JSON payload omits the redundant type field (golden 85).
func TestR6aSSEEventHeaderCohereOTLP(t *testing.T) {
	const stream = "event: message-start\ndata: {\"id\":\"co-1\",\"delta\":{\"message\":{\"role\":\"assistant\"}}}\n\n" +
		"event: content-delta\ndata: {\"index\":0,\"delta\":{\"message\":{\"content\":{\"text\":\"VISIBLE_COHERE_EVENT_TEXT\"}}}}\n\n" +
		"event: message-end\ndata: {\"delta\":{\"finish_reason\":\"COMPLETE\",\"usage\":{\"tokens\":{\"input_tokens\":3,\"output_tokens\":2}}}}\n\n"
	span, app := r6aDifferential(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, stream)
	}, "http://api.cohere.com/v2/chat", `{"model":"command-r","messages":[{"role":"user","content":"hi"}],"stream":true}`, nil)
	if app.data != stream || app.err != "" {
		t.Fatal("application stream changed")
	}
	attrs := r6aAttrs(span)
	if got := attrs["output.value"].GetStringValue(); got != "VISIBLE_COHERE_EVENT_TEXT" {
		t.Fatalf("output.value=%q", got)
	}
	if attrs["gen_ai.response.id"].GetStringValue() != "co-1" || attrs["gen_ai.usage.total_tokens"].GetIntValue() != 5 || attrs["gen_ai.response.time_to_first_text"] == nil {
		t.Fatalf("event-header-only Cohere stream lost id/usage/first-text: %v", attrs)
	}
	for _, kv := range span.Attributes {
		if strings.Contains(kv.Value.String(), `"event"`) {
			t.Fatalf("SSE event name leaked into %s", kv.Key)
		}
	}
}

// go-s6 final #4 / A10: time-to-first-text comes from the transport arrival of
// the chunk completing the first non-empty output event, recorded before
// parsing. Role-only and empty events establish first-chunk only.
func TestR6aFirstTextFromArrivalOTLP(t *testing.T) {
	const gap = 80 * time.Millisecond
	release := make(chan struct{})
	role := "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"\"}}]}\n\n"
	text := "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"nonempty\"}}]}\n\n"
	done := "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":1,\"completion_tokens\":1,\"total_tokens\":2}}\n\ndata: [DONE]\n\n"
	var runs atomic.Int32
	read := func(r io.Reader) ([]byte, error) {
		// Read the role-only chunk, wait, then let the server send output.
		var out bytes.Buffer
		buf := make([]byte, len(role))
		n, err := io.ReadFull(r, buf)
		out.Write(buf[:n])
		if err != nil {
			return out.Bytes(), err
		}
		time.Sleep(gap)
		release <- struct{}{}
		rest, err := io.ReadAll(r)
		out.Write(rest)
		return out.Bytes(), err
	}
	span, app := r6aDifferential(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		runs.Add(1)
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, role)
		w.(http.Flusher).Flush()
		<-release
		_, _ = io.WriteString(w, text+done)
	}, "http://api.openai.com/v1/chat/completions", `{"model":"m","messages":[{"role":"user","content":"hi"}],"stream":true}`, read)
	if app.data != role+text+done || runs.Load() != 2 {
		t.Fatal("application stream changed")
	}
	attrs := r6aAttrs(span)
	first, chunk := attrs["gen_ai.response.time_to_first_text"], attrs["gen_ai.response.time_to_first_chunk"]
	if first == nil || chunk == nil {
		t.Fatalf("milestones missing: first_text=%v first_chunk=%v", first, chunk)
	}
	duration := float64(span.EndTimeUnixNano-span.StartTimeUnixNano) / 1e9
	if first.GetDoubleValue()-chunk.GetDoubleValue() < gap.Seconds()*0.9 || first.GetDoubleValue() > duration+1e-6 {
		t.Fatalf("first_text=%v first_chunk=%v duration=%v: not the output chunk's arrival", first, chunk, duration)
	}
	if attrs["output.value"].GetStringValue() != "nonempty" || attrs["traceai.wire.incomplete"] != nil {
		t.Fatal("output or completeness changed")
	}
}

// A10 bounded association: past the record cap the transport stops recording.
// A first output beyond the recorded prefix is omitted with the incomplete
// marker, never replaced by parse time or first-chunk time.
func TestR6aArrivalAssociationBoundOTLP(t *testing.T) {
	role := "data: {\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\"}}]}\n\n"
	var stream strings.Builder
	for i := 0; i < 4100; i++ {
		stream.WriteString(role)
	}
	stream.WriteString("data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"late\"}}]}\n\n")
	body := stream.String()
	read := func(r io.Reader) ([]byte, error) {
		// One small Read per transport observation, as a byte-wise consumer does.
		var out bytes.Buffer
		buf := make([]byte, len(role))
		for {
			n, err := r.Read(buf)
			out.Write(buf[:n])
			if err == io.EOF {
				return out.Bytes(), nil
			}
			if err != nil {
				return out.Bytes(), err
			}
		}
	}
	span, app := r6aDifferential(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, body)
	}, "http://api.openai.com/v1/chat/completions", `{"model":"m","messages":[{"role":"user","content":"hi"}],"stream":true}`, read)
	if app.data != body {
		t.Fatal("application stream changed")
	}
	attrs := r6aAttrs(span)
	if attrs["gen_ai.response.time_to_first_text"] != nil || attrs["traceai.wire.incomplete"].GetBoolValue() != true || attrs["gen_ai.response.time_to_first_chunk"] == nil {
		t.Fatalf("lost association must omit first-text and mark incomplete: %v", attrs["traceai.wire.incomplete"])
	}
	if attrs["output.value"].GetStringValue() != "late" {
		t.Fatal("output must still be parsed")
	}
}
