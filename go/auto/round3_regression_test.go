package auto_test

import (
	"bytes"
	"context"
	"errors"
	"io"
	"mime/multipart"
	"net"
	"net/http"
	"net/http/httptest"
	"net/textproto"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/future-agi/traceAI/go/auto"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
)

func TestFilenameLessAudioTransport(t *testing.T) {
	rec, _ := regressionSetup(t, auto.TraceConfig{})
	var body bytes.Buffer
	w := multipart.NewWriter(&body)
	p, _ := w.CreatePart(textproto.MIMEHeader{"Content-Disposition": {`form-data; name="file"`}, "Content-Type": {"audio/wav; codec=pcm"}})
	private := "RIFF\x00RAW_AUDIO_PRIVATE\x00WAVE"
	_, _ = io.WriteString(p, private)
	_ = w.WriteField("model", "whisper-1")
	_ = w.Close()
	raw := body.String()
	tr := auto.Transport(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		got, err := io.ReadAll(r.Body)
		_ = r.Body.Close()
		if err != nil || string(got) != raw {
			t.Error("application multipart changed")
		}
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}}, Body: io.NopCloser(strings.NewReader(`{"text":"hello"}`))}, nil
	}))
	r, _ := http.NewRequest("POST", "https://api.openai.com/v1/audio/transcriptions", strings.NewReader(raw))
	r.Header.Set("Content-Type", w.FormDataContentType())
	resp, err := tr.RoundTrip(r)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	ss := wires(rec)
	if len(ss) != 1 || value(ss[0], "traceai.audio.input_bytes") != int64(len(private)) {
		t.Fatalf("count-only audio metadata lost: %v", ss)
	}
	for _, kv := range ss[0].Attributes() {
		if strings.Contains(kv.Value.Emit(), "RAW_AUDIO_PRIVATE") {
			t.Fatalf("binary audio exported in %s", kv.Key)
		}
	}
}

func TestContextStandardPrivacy(t *testing.T) {
	for _, tc := range []struct {
		name string
		cfg  auto.TraceConfig
		keys []string
	}{
		{"inputs", auto.TraceConfig{HideInputs: true}, []string{"gen_ai.system_instructions", "gen_ai.tool.call.arguments"}},
		{"input-text", auto.TraceConfig{HideInputText: true}, []string{"gen_ai.system_instructions", "gen_ai.tool.call.arguments"}},
		{"input-messages", auto.TraceConfig{HideInputMessages: true}, []string{"gen_ai.system_instructions", "gen_ai.tool.call.arguments"}},
		{"outputs", auto.TraceConfig{HideOutputs: true}, []string{"gen_ai.tool.call.result"}},
		{"output-text", auto.TraceConfig{HideOutputText: true}, []string{"gen_ai.tool.call.result"}},
		{"output-messages", auto.TraceConfig{HideOutputMessages: true}, []string{"gen_ai.tool.call.result"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rec, _ := regressionSetup(t, tc.cfg)
			ctx := context.Background()
			for _, key := range tc.keys {
				ctx = auto.ContextWithAttributes(ctx, attribute.StringSlice(key, []string{"PRIVATE_CONTEXT_TEXT"}))
			}
			regressionExchange(t, regressionTransport(responseJSON, "application/json", 200), ctx, "https://api.openai.com/v1/chat/completions", requestJSON, nil)
			for _, kv := range wires(rec)[0].Attributes() {
				if strings.Contains(kv.Value.Emit(), "PRIVATE_CONTEXT_TEXT") {
					t.Fatalf("hidden standard context content in %s", kv.Key)
				}
			}
		})
	}
}

type legacyCancelTransport struct {
	entered  chan *http.Request
	canceled chan struct{}
	release  chan struct{}
	once     sync.Once
	mu       sync.Mutex
	current  *http.Request
}

func (l *legacyCancelTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	l.mu.Lock()
	l.current = r
	l.mu.Unlock()
	l.entered <- r
	select {
	case <-l.canceled:
	case <-l.release:
	}
	return nil, errors.New("legacy transport canceled")
}
func (l *legacyCancelTransport) CancelRequest(r *http.Request) {
	l.mu.Lock()
	same := r == l.current
	l.mu.Unlock()
	if same {
		l.once.Do(func() { close(l.canceled) })
	}
}
func TestLegacyCancellationPreserved(t *testing.T) {
	regressionSetup(t, auto.TraceConfig{})
	for _, path := range []string{"/unrelated", "/v1/chat/completions"} {
		t.Run(path, func(t *testing.T) {
			base := &legacyCancelTransport{entered: make(chan *http.Request, 1), canceled: make(chan struct{}), release: make(chan struct{})}
			defer close(base.release)
			cancel := make(chan struct{})
			r, _ := http.NewRequest("POST", "https://api.openai.com"+path, strings.NewReader(requestJSON))
			r.Cancel = cancel
			// Enable http.Client's legacy timeout/cancellation bridge, then trigger
			// that bridge synchronously rather than racing a short wall clock.
			client := &http.Client{Transport: auto.Transport(base), Timeout: time.Hour}
			done := make(chan error, 1)
			go func() { _, err := client.Do(r); done <- err }()
			<-base.entered
			close(cancel)
			select {
			case <-base.canceled:
			case <-time.After(2 * time.Second):
				t.Fatal("CancelRequest interface or original-to-clone mapping lost")
			}
			select {
			case err := <-done:
				if err == nil {
					t.Fatal("cancellation ignored")
				}
			case <-time.After(2 * time.Second):
				t.Fatal("legacy client remained blocked")
			}
		})
	}
}

type staleWriteConn struct {
	net.Conn
	armed *atomic.Bool
}

func (c *staleWriteConn) Write(p []byte) (int, error) {
	if c.armed.CompareAndSwap(true, false) {
		return 0, errors.New("injected stale-connection write failure")
	}
	return c.Conn.Write(p)
}
func TestTransportGetBodyRetryObserved(t *testing.T) {
	for _, mode := range []string{"known", "proxy", "multipart"} {
		t.Run(mode, func(t *testing.T) {
			rec, _ := regressionSetup(t, auto.TraceConfig{})
			body, ct, path := requestJSON, "application/json", "/v1/chat/completions"
			if mode == "multipart" {
				var b bytes.Buffer
				w := multipart.NewWriter(&b)
				_ = w.WriteField("model", "whisper-1")
				part, _ := w.CreateFormFile("file", "audio.wav")
				_, _ = io.WriteString(part, "PRIVATE_AUDIO_BYTES")
				_ = w.Close()
				body = b.String()
				ct = w.FormDataContentType()
				path = "/v1/audio/transcriptions"
			}
			serverBodies := make(chan string, 2)
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				got, err := io.ReadAll(r.Body)
				if err != nil {
					t.Error(err)
				}
				serverBodies <- string(got)
				w.Header().Set("Content-Type", "application/json")
				_, _ = io.WriteString(w, `{"text":"hello","choices":[{"message":{"role":"assistant","content":"hello"}}]}`)
			}))
			defer srv.Close()
			var dials atomic.Int32
			var armed atomic.Bool
			base := &http.Transport{DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
				conn, err := (&net.Dialer{}).DialContext(ctx, network, srv.Listener.Addr().String())
				if err != nil {
					return nil, err
				}
				if dials.Add(1) == 1 {
					return &staleWriteConn{conn, &armed}, nil
				}
				return conn, nil
			}}
			defer base.CloseIdleConnections()
			client := &http.Client{Transport: auto.Transport(base)}
			host := "api.openai.com"
			if mode == "proxy" {
				host = "proxy.example"
			}
			warm, err := client.Get("http://" + host + "/warm")
			if err != nil {
				t.Fatal(err)
			}
			_, _ = io.Copy(io.Discard, warm.Body)
			_ = warm.Body.Close()
			<-serverBodies
			armed.Store(true) // The first connection is idle/reused, not timing-based.
			r, _ := http.NewRequest("POST", "http://"+host+path, strings.NewReader(body))
			r.Header.Set("Content-Type", ct)
			getBody := r.GetBody
			var replays atomic.Int32
			r.GetBody = func() (io.ReadCloser, error) { replays.Add(1); return getBody() }
			resp, err := client.Do(r)
			if err != nil {
				t.Fatal(err)
			}
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			if got := <-serverBodies; got != body {
				t.Fatal("transport replay changed body")
			}
			if dials.Load() != 2 || replays.Load() != 1 {
				t.Fatalf("retry not exercised: dials=%d replays=%d", dials.Load(), replays.Load())
			}
			ss := wires(rec)
			if len(ss) != 1 || value(ss[0], "input.value") == nil || value(ss[0], "traceai.wire.body_unavailable") != nil {
				t.Fatalf("successful retry capture lost: %v", ss)
			}
			if mode == "multipart" && value(ss[0], "traceai.audio.input_bytes") != int64(len("PRIVATE_AUDIO_BYTES")) {
				t.Fatal("replay multipart not counted")
			}
		})
	}
}

type panicReadBody struct {
	first      string
	token      any
	closePanic bool
}

func (b *panicReadBody) Read(p []byte) (int, error) {
	if b.first != "" {
		n := copy(p, b.first)
		b.first = b.first[n:]
		return n, nil
	}
	panic(b.token)
}
func (b *panicReadBody) Close() error {
	if b.closePanic {
		panic(b.token)
	}
	return errors.New("consumer close error")
}
func TestConsumerAbortAndCompleteBodies(t *testing.T) {
	for _, mode := range []string{"partial-panic", "read-panic", "eof-close-panic", "length-close-panic", "length-close-error"} {
		t.Run(mode, func(t *testing.T) {
			rec, _ := regressionSetup(t, auto.TraceConfig{})
			token := &struct{ label string }{"application-panic"}
			sse := "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hello\"}}]}\n\n"
			var body io.ReadCloser = &panicReadBody{first: sse, token: token, closePanic: strings.Contains(mode, "close-panic")}
			if mode == "eof-close-panic" {
				body = &eofClosePanicBody{Reader: strings.NewReader(sse), token: token}
			}
			length := int64(-1)
			if strings.HasPrefix(mode, "length") {
				length = int64(len(sse))
			}
			tr := auto.Transport(roundTripFunc(func(r *http.Request) (*http.Response, error) {
				_, _ = io.Copy(io.Discard, r.Body)
				_ = r.Body.Close()
				return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"text/event-stream"}}, Body: body, ContentLength: length}, nil
			}))
			r, _ := http.NewRequest("POST", "https://api.openai.com/v1/chat/completions", strings.NewReader(requestJSON))
			r.Header.Set("Content-Type", "application/json")
			resp, err := tr.RoundTrip(r)
			if err != nil {
				t.Fatal(err)
			}
			var recovered any
			func() {
				defer func() { recovered = recover() }()
				defer resp.Body.Close()
				if mode == "eof-close-panic" {
					_, _ = io.ReadAll(resp.Body)
					return
				}
				buf := make([]byte, len(sse))
				n, err := io.ReadFull(resp.Body, buf)
				if n != len(sse) || err != nil || string(buf) != sse {
					t.Error("body bytes changed")
				}
				if mode == "partial-panic" {
					panic(token)
				}
				if mode == "read-panic" {
					_, _ = resp.Body.Read(buf)
				}
			}()
			if mode != "length-close-error" && recovered != token {
				t.Fatalf("application panic changed: %v", recovered)
			}
			ss := wires(rec)
			if len(ss) != 1 || ss[0].Status().Code != codes.Ok || value(ss[0], "error.type") != nil || value(ss[0], "output.value") != "hello" {
				t.Fatalf("consumer abort changed span: %v", ss)
			}
			incomplete := mode == "partial-panic" || mode == "read-panic"
			if (value(ss[0], "traceai.wire.incomplete") == true) != incomplete {
				t.Fatalf("completion marker wrong for %s", mode)
			}
		})
	}
}

type eofClosePanicBody struct {
	*strings.Reader
	token any
}

func (b *eofClosePanicBody) Close() error { panic(b.token) }
