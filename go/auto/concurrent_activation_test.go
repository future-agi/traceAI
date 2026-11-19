package auto

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

// Two AI requests reach the armed default transport on different goroutines
// while the first one is still activating the agent. Both are captured: the
// second waits for activation instead of passing through untraced. The test
// parks the first request inside activation by holding the known-secret
// registry's lock, which activation takes when it registers the FI keys, and
// sends the second request while the first is parked. Run it with -race.
func TestConcurrentFirstAIRequestsAreBothCaptured(t *testing.T) {
	ownedRegressionSetup(t)
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.Header().Set("Content-Type", "application/x-protobuf")
	}))
	t.Cleanup(sink.Close)
	t.Setenv("FI_BASE_URL", sink.URL)
	// Eight characters or more, so activation registers them as known secrets.
	t.Setenv("FI_API_KEY", "CONCURRENT_FI_KEY")
	t.Setenv("FI_SECRET_KEY", "CONCURRENT_FI_SECRET")
	rec := tracetest.NewSpanRecorder()
	p := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
	t.Cleanup(func() { _ = p.Shutdown(context.Background()) })
	otel.SetTracerProvider(p) // ownedRegressionSetup restores the previous one.
	base := http.DefaultTransport
	http.DefaultTransport = round5SuccessTransport()
	t.Cleanup(func() { http.DefaultTransport = base })
	agent.Lock()
	agent.first = nil
	agent.repeated = sync.Once{}
	agent.Unlock()
	arm()
	agent.Lock()
	armed := agent.armed
	agent.Unlock()
	if armed == nil {
		t.Fatal("package initialization did not arm wire capture")
	}
	wrapper := http.DefaultTransport

	var unparked sync.Once
	processSecrets.mu.Lock()
	unpark := func() { unparked.Do(processSecrets.mu.Unlock) }
	defer unpark()

	var wg sync.WaitGroup
	send := func() {
		defer wg.Done()
		req, _ := http.NewRequest("POST", "https://api.openai.com/v1/chat/completions", strings.NewReader(round4Request))
		resp, err := wrapper.RoundTrip(req)
		if err != nil {
			t.Error(err)
			return
		}
		_, _ = io.Copy(io.Discard, resp.Body)
		_ = resp.Body.Close()
	}
	wg.Add(2)
	go send()
	// The first request holds the agent lock while it activates the agent.
	deadline := time.Now().Add(5 * time.Second)
	for agent.TryLock() {
		agent.Unlock()
		if time.Now().After(deadline) {
			t.Fatal("the first AI request did not start activation")
		}
		time.Sleep(time.Millisecond)
	}
	// Let it reach the registry lock, send the second request while it is
	// parked there, and give that request time to reach the wrapper.
	time.Sleep(50 * time.Millisecond)
	go send()
	time.Sleep(100 * time.Millisecond)
	unpark()
	wg.Wait()
	wires := 0
	for _, span := range rec.Ended() {
		if span.InstrumentationScope().Name == wireScope {
			wires++
		}
	}
	if wires != 2 {
		t.Fatalf("want a wire span for each of the two AI requests, got %d", wires)
	}
	_ = Shutdown(context.Background())
}
