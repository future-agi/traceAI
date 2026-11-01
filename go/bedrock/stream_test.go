package traceaibedrock

import (
	"context"
	"errors"
	"net"
	"net/http"
	"sync"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"
	"go.opentelemetry.io/otel/codes"
)

func TestStreamTerminalError(t *testing.T) {
	for _, mode := range []string{"waiting", "forwarding", "eof", "close"} {
		for _, sdkError := range []bool{false, true} {
			name := mode + "/context-error"
			if sdkError {
				name = mode + "/sdk-error"
			}
			t.Run(name, func(t *testing.T) {
				p, e := provider(t)
				ctx, cancel := context.WithCancel(context.Background())
				defer cancel()
				_, r := NewClient(nil, WithTracerProvider(p)).start(ctx, "test", nil)
				inner := &fakeReader{events: make(chan *int, 1), closed: make(chan struct{})}
				want := error(context.Canceled)
				if sdkError {
					want = errors.New("private SDK error")
					inner.err = want
				}
				observing, resume := make(chan struct{}), make(chan struct{})
				if mode != "waiting" {
					value := 1
					inner.events <- &value
				}
				s := observeStream(ctx, inner, r, func(*int) { close(observing); <-resume })
				defer s.Close()
				if mode != "waiting" {
					<-observing
				}
				// Err may be queried while the pump publishes its terminal error.
				var readers sync.WaitGroup
				readers.Go(func() {
					for {
						select {
						case <-s.stopped:
							return
						default:
							_ = s.Err()
						}
					}
				})
				cancel()
				switch mode {
				case "eof":
					close(inner.events)
				case "close":
					s.closeInner()
				}
				close(resume)
				for range s.Events() {
				}
				readers.Wait()
				if got := s.Err(); got != want {
					t.Errorf("terminal error = %v, want original %v", got, want)
				}
				if got := s.Close(); got != inner.err {
					t.Errorf("Close changed SDK result: %v", got)
				}
				if inner.calls.Load() != 1 || len(llms(e)) != 1 || llms(e)[0].Status.Code != codes.Error {
					t.Fatal("stream did not close and end one error span")
				}
			})
		}
	}
}

func TestCompletedStreamIgnoresLaterCancellation(t *testing.T) {
	for _, explicitClose := range []bool{false, true} {
		p, e := provider(t)
		ctx, cancel := context.WithCancel(context.Background())
		_, r := NewClient(nil, WithTracerProvider(p)).start(ctx, "test", nil)
		inner := &fakeReader{events: make(chan *int), closed: make(chan struct{})}
		if !explicitClose {
			close(inner.events)
		}
		s := observeStream(ctx, inner, r, func(*int) {})
		if explicitClose {
			_ = s.Close()
		}
		for range s.Events() {
		}
		cancel()
		if s.Err() != nil || s.Close() != nil || llms(e)[0].Status.Code == codes.Error {
			t.Fatal("later cancellation changed a completed stream")
		}
	}
}

func TestSDKStreamCancellationErrorParity(t *testing.T) {
	for _, operation := range []string{"ConverseStream", "InvokeModelWithResponseStream"} {
		for _, wrapped := range []bool{false, true} {
			name := operation + "/native"
			if wrapped {
				name = operation + "/observed"
			}
			t.Run(name, func(t *testing.T) {
				p, e := provider(t)
				newRaw := func() *bedrockruntime.Client {
					return rawClient(t, func(w http.ResponseWriter, r *http.Request) {
						w.Header().Set("Content-Type", "application/vnd.amazon.eventstream")
						if operation == "ConverseStream" {
							_ = frame(w, "contentBlockDelta", `{"contentBlockIndex":0,"delta":{"text":"hello"}}`)
						} else {
							invokeFrames(w)
						}
						w.(http.Flusher).Flush()
						<-r.Context().Done()
					})
				}
				var raw *bedrockruntime.Client
				if wrapped {
					raw = newRaw()
				}
				for i := 0; i < 30; i++ {
					// The adapter safely reuses a client even while canceled native
					// decoders finish asynchronously. Native-only calls still need
					// independent clients on the affected upstream SDK.
					if !wrapped {
						raw = newRaw()
					}
					client := NewClient(raw, WithTracerProvider(p))
					ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
					if operation == "ConverseStream" {
						in := &bedrockruntime.ConverseStreamInput{ModelId: aws.String(model), Messages: input().Messages}
						if wrapped {
							out, err := client.ConverseStream(ctx, in)
							if err != nil {
								cancel()
								t.Fatal(err)
							}
							assertCanceledStream(t, cancel, out.GetStream())
						} else {
							out, err := raw.ConverseStream(ctx, in)
							if err != nil {
								cancel()
								t.Fatal(err)
							}
							assertCanceledStream(t, cancel, out.GetStream())
						}
					} else {
						in := &bedrockruntime.InvokeModelWithResponseStreamInput{ModelId: aws.String(model), Body: []byte(invokeBody), ContentType: aws.String("application/json")}
						if wrapped {
							out, err := client.InvokeModelWithResponseStream(ctx, in)
							if err != nil {
								cancel()
								t.Fatal(err)
							}
							assertCanceledStream(t, cancel, out.GetStream())
						} else {
							out, err := raw.InvokeModelWithResponseStream(ctx, in)
							if err != nil {
								cancel()
								t.Fatal(err)
							}
							assertCanceledStream(t, cancel, out.GetStream())
						}
					}
				}
				if wrapped {
					if spans := llms(e); len(spans) != 30 {
						t.Fatalf("want 30 ended spans, got %d", len(spans))
					} else {
						for _, s := range spans {
							if s.Status.Code != codes.Error {
								t.Error("canceled stream marked successful")
							}
						}
					}
				}
			})
		}
	}
}

func assertCanceledStream[T any](t *testing.T, cancel context.CancelFunc, s reader[T]) {
	t.Helper()
	defer s.Close()
	defer cancel()
	if _, ok := <-s.Events(); !ok {
		t.Fatal("stream ended before first event")
	}
	cancel()
	for range s.Events() {
	}
	// The native SDK can also surface net.ErrClosed when cancellation races
	// the transport read. Preserve that genuine error rather than replacing it.
	if err := s.Err(); !errors.Is(err, context.Canceled) && !errors.Is(err, net.ErrClosed) {
		t.Errorf("incomplete canceled stream error = %v, want cancellation or native closed-connection error", err)
	}
}
