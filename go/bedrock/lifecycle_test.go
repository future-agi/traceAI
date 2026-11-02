package traceaibedrock

import (
	"context"
	"errors"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime/types"
	"github.com/aws/smithy-go/middleware"
	"github.com/aws/smithy-go/tracing/smithyoteltracing"
	"go.opentelemetry.io/otel/attribute"
)

type fakeReader struct {
	events chan *int
	closed chan struct{}
	once   sync.Once
	calls  atomic.Int32
	err    error
}

func (f *fakeReader) Events() <-chan *int { return f.events }
func (f *fakeReader) Err() error          { return f.err }
func (f *fakeReader) Close() error {
	f.calls.Add(1)
	f.once.Do(func() { close(f.closed) })
	return f.err
}
func TestReaderIdentityErrorAndNoRetainedPump(t *testing.T) {
	for n := 0; n < 50; n++ {
		p, e := provider(t)
		_, r := NewClient(nil, WithTracerProvider(p)).start(context.Background(), "test", nil)
		sentinel := errors.New("secret sentinel")
		inner := &fakeReader{events: make(chan *int), closed: make(chan struct{}), err: sentinel}
		s := observeStream(context.Background(), inner, r, func(*int) {})
		value := 7
		go func() {
			select {
			case inner.events <- &value:
			case <-inner.closed:
			}
		}()
		if got := <-s.Events(); got != &value {
			t.Fatal("event identity changed")
		}
		var wg sync.WaitGroup
		for i := 0; i < 8; i++ {
			wg.Go(func() {
				if s.Close() != sentinel {
					t.Error("close error identity changed")
				}
			})
		}
		wg.Wait()
		if s.Err() != sentinel || inner.calls.Load() != 1 {
			t.Fatal("error or ownership changed")
		}
		select {
		case <-s.stopped:
		default:
			t.Fatal("pump outlived close")
		}
		if len(llms(e)) != 1 {
			t.Fatal("span not ended once")
		}
	}
}
func TestNativeStreamEndsAtHeaders(t *testing.T) {
	p, e := provider(t)
	raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.amazon.eventstream")
		_ = frame(w, "messageStart", `{"role":"assistant"}`)
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	}, func(o *bedrockruntime.Options) { o.TracerProvider = smithyoteltracing.Adapt(p) })
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	out, err := raw.ConverseStream(ctx, &bedrockruntime.ConverseStreamInput{ModelId: aws.String(model), Messages: input().Messages})
	if err != nil {
		t.Fatal(err)
	}
	defer out.GetStream().Close()
	if len(e.GetSpans()) == 0 {
		t.Fatal("native baseline missing")
	}
	if len(llms(e)) != 0 {
		t.Fatal("native already semantic")
	}
	// AWS has exported RPC spans even though the body is still blocked.
	for _, s := range e.GetSpans() {
		if !s.EndTime.After(s.StartTime) {
			t.Fatal("invalid native span")
		}
	}
}
func TestAPIOptionsErrorIdentity(t *testing.T) {
	p, e := provider(t)
	sentinel := errors.New("private setup detail")
	raw := rawClient(t, func(w http.ResponseWriter, r *http.Request) { t.Error("setup failure reached network") })
	_, err := NewClient(raw, WithTracerProvider(p)).Converse(context.Background(), input(), func(o *bedrockruntime.Options) {
		o.APIOptions = append(o.APIOptions, func(*middleware.Stack) error { return sentinel })
	})
	if !errors.Is(err, sentinel) || len(llms(e)) != 1 {
		t.Fatal("SDK error chain lost")
	}
	if strings.Contains(llms(e)[0].Status.Description, "private") {
		t.Fatal("setup detail leaked")
	}
}
func TestMissingZeroUsageAndCaptureSlotBudget(t *testing.T) {
	p, e := provider(t)
	_, r := NewClient(nil, WithTracerProvider(p)).start(context.Background(), "test", nil)
	r.converseInput([]types.Message{{Role: types.ConversationRoleUser}}, nil, nil, nil)
	if r.first {
		t.Fatal("input recorded TTFT")
	}
	r.converseEvent(&types.ConverseStreamOutputMemberMessageStart{Value: types.MessageStartEvent{Role: types.ConversationRoleAssistant}})
	if r.first {
		t.Fatal("role event recorded TTFT")
	}
	for i := 0; i < maxBlocks; i++ {
		b := r.ensureBlock(int32(i), "tool_use")
		b.ID = "id"
		b.Name = "name"
		b.Arguments = "{}"
	}
	r.setUsage(&types.TokenUsage{InputTokens: aws.Int32(0), CacheWriteInputTokens: aws.Int32(0)})
	r.stop("secret-stop-reason")
	r.finish(nil, false)
	a := attrs(llms(e)[0])
	if a["gen_ai.usage.input_tokens"].Type() != attribute.INT64 || a["gen_ai.usage.input_tokens"].AsInt64() != 0 {
		t.Fatal("zero lost")
	}
	if x := a["gen_ai.usage.cache_write_tokens"]; x.Type() != attribute.INT64 || x.AsInt64() != 0 {
		t.Fatal("zero cache-write tokens lost")
	}
	if _, ok := a["gen_ai.usage.output_tokens"]; ok {
		t.Fatal("missing output became zero")
	}
	if _, ok := a["gen_ai.response.finish_reasons"]; ok {
		t.Fatal("arbitrary stop text exported")
	}
	if !a["traceai.capture.truncated"].AsBool() || llms(e)[0].DroppedAttributes != 0 {
		t.Fatal("usage not protected from attribute cap")
	}
}
