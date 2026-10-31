package auto

import (
	"context"
	"io"
	"net/http"
	"net/http/httptrace"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	contexttransport "github.com/future-agi/traceAI/go/auto/internal/http2"
	"github.com/future-agi/traceAI/go/auto/internal/wire"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
	"golang.org/x/net/http2"
)

type wireTransport struct {
	base     http.RoundTripper
	state    *agentState
	mu       sync.Mutex
	requests map[*http.Request]*http.Request
}

// Legacy transports use this interface instead of Request.Context. The client
// cancels its original request, whereas the underlying transport sees our clone.
func (t *wireTransport) CancelRequest(req *http.Request) {
	t.mu.Lock()
	outgoing := t.requests[req]
	t.mu.Unlock()
	if outgoing == nil {
		outgoing = req
	}
	if canceler, ok := t.base.(interface{ CancelRequest(*http.Request) }); ok {
		canceler.CancelRequest(outgoing)
	}
}

func unwrapTransport(base http.RoundTripper) http.RoundTripper {
	if t, ok := base.(*contexttransport.Transport); ok {
		return t.RoundTripper
	}
	return base
}

// Preserve http.Client's native context-only timeout path at installation, not
// by rewriting errors or racing an extra cancellation goroutine at request time.
func wrapTransport(base http.RoundTripper, state *agentState) http.RoundTripper {
	if native, ok := base.(*http.Transport); ok {
		state.observeNativeTransport(native)
	}
	t := &wireTransport{base: base, state: state}
	switch base.(type) {
	case *http.Transport, *http2.Transport:
		return &contexttransport.Transport{RoundTripper: t}
	default:
		return t // Unknown/legacy transports retain all three cancellation paths.
	}
}
func (t *wireTransport) track(req, outgoing *http.Request) func() {
	t.mu.Lock()
	if t.requests == nil {
		t.requests = make(map[*http.Request]*http.Request)
	}
	t.requests[req] = outgoing
	t.mu.Unlock()
	return func() { t.mu.Lock(); delete(t.requests, req); t.mu.Unlock() }
}

// Preserve http.Client.CloseIdleConnections through the wrapper.
func (t *wireTransport) CloseIdleConnections() {
	if closer, ok := t.base.(interface{ CloseIdleConnections() }); ok {
		closer.CloseIdleConnections()
	}
}

// Transport covers a custom client's transport. If the agent is disabled it
// returns base unchanged. Build clients after Init; never mutate transports
// concurrently with requests.
func Transport(base http.RoundTripper) http.RoundTripper {
	if base == nil {
		base = http.DefaultTransport
	}
	if t, ok := unwrapTransport(base).(*wireTransport); ok {
		if t.state.active.Load() {
			return base
		}
		base = t.base
	}
	agent.Lock()
	s := agent.current
	agent.Unlock()
	if s == nil || !s.config.WireCapture || !allowed(s.config, "wire") {
		return base
	}
	return wrapTransport(base, s)
}

// The nested-capture marker has a lifetime, not a permanent boolean. A context
// retained by Response.Request must not suppress later application exchanges.
type captureFlight struct{ active atomic.Bool }

func suppressed(ctx context.Context) bool {
	if ctx.Value(suppressionKey) == true {
		return true
	}
	flight, _ := ctx.Value(captureKey).(*captureFlight)
	return flight != nil && flight.active.Load()
}
func (t *wireTransport) skip(ctx context.Context) bool {
	if suppressed(ctx) {
		return true
	}
	if s, ok := trace.SpanFromContext(ctx).(sdktrace.ReadOnlySpan); ok && modelSpan(s) {
		return true
	}
	return t.state.registration.covered(ctx)
}

func observeRequestCredentials(req *http.Request) {
	if req == nil || req.URL == nil {
		return
	}
	_ = guarded(func() error {
		for _, value := range wire.CredentialValues(req.Header, req.URL.String()) {
			RegisterSecret(value)
		}
		return nil
	})
}

type credentialTraceKey struct{}

// Native transports see the authenticated clone even when an arbitrary auth
// RoundTripper sits inside our wrapper. httptrace observes those actual headers
// before any response/error can be exported; it never reads a body or changes
// credentials. WithClientTrace composes (rather than replaces) application hooks.
func (t *wireTransport) credentialContext(ctx context.Context) context.Context {
	if ctx.Value(credentialTraceKey{}) == t.state {
		return ctx // One observer for nested hooks on the same request context.
	}
	ctx = context.WithValue(ctx, credentialTraceKey{}, t.state)
	return httptrace.WithClientTrace(ctx, &httptrace.ClientTrace{
		WroteHeaderField: func(name string, values []string) {
			if !t.state.active.Load() {
				return
			}
			_ = guarded(func() error {
				for _, value := range wire.HeaderCredentialValues(name, values) {
					RegisterSecret(value)
				}
				// HTTP/2 exposes the actual request target as a pseudo-header.
				if name == ":path" {
					for _, target := range values {
						for _, value := range wire.CredentialValues(nil, target) {
							RegisterSecret(value)
						}
					}
				}
				return nil
			})
		},
	})
}

// No matching wire span (including S4 suppression): only attach the credential
// observer. A shallow request copy preserves Body/GetBody, headers and trailers.
func (t *wireTransport) roundTripWithoutCapture(req *http.Request) (*http.Response, error) {
	ctx := t.credentialContext(req.Context())
	outgoing := req
	if ctx != req.Context() {
		outgoing = req.WithContext(ctx)
	}
	var untrack func()
	if _, legacy := t.base.(interface{ CancelRequest(*http.Request) }); legacy && outgoing != req {
		untrack = t.track(req, outgoing)
	}
	defer func() {
		if untrack != nil {
			untrack() // Includes transport errors/panics and no-body responses.
		}
	}()
	resp, err := t.base.RoundTrip(outgoing)
	if resp != nil {
		// Native responses retain the final request, including an auth wrapper's
		// changed URL. Harvest it before returning any response to the caller.
		observeRequestCredentials(resp.Request)
		if resp.Request == outgoing {
			resp.Request = req
		}
		if err == nil && untrack != nil && resp.StatusCode != http.StatusSwitchingProtocols && resp.Body != nil && resp.Body != http.NoBody {
			// Legacy cancellation may occur during response reads, not just before
			// headers. This observer delegates bytes/errors without capturing them.
			resp.Body = &observationBody{ReadCloser: resp.Body, release: untrack}
			untrack = nil
		}
	}
	return resp, err
}

type observationBody struct {
	io.ReadCloser
	once    sync.Once
	release func()
}

func (b *observationBody) Read(p []byte) (n int, err error) {
	defer func() {
		if v := recover(); v != nil {
			b.once.Do(b.release)
			panic(v)
		}
	}()
	n, err = b.ReadCloser.Read(p)
	if err != nil {
		b.once.Do(b.release)
	}
	return n, err
}
func (b *observationBody) Close() error {
	defer b.once.Do(b.release)
	return b.ReadCloser.Close()
}

type bounded struct {
	data              []byte
	size              int64
	max               int64
	binary, truncated bool
}

func (b *bounded) write(p []byte) {
	b.size += int64(len(p))
	if b.binary {
		return
	}
	if int64(len(b.data))+int64(len(p)) > b.max {
		b.truncated = true
		b.data = nil
		return
	}
	if !b.truncated {
		b.data = append(b.data, p...)
	}
}

// requestObserver sees only bytes the underlying transport actually reads. This
// preserves request-body errors, cancellation, redirects and streaming uploads.
// Multipart audio goes straight to a counting parser, never a body buffer.
type requestObserver struct {
	mu                sync.Mutex
	capture           bounded
	fields            map[string]string
	files             []wire.FilePart
	pipe              *io.PipeWriter
	done              chan struct{}
	once              sync.Once
	complete          bool
	released          bool
	undecodedEncoding string
}

func newObserver(ct, encoding string, max int64) *requestObserver {
	o := &requestObserver{capture: bounded{max: max, binary: wire.Binary(strings.ToLower(strings.TrimSpace(strings.Split(ct, ";")[0])))}}
	if strings.HasPrefix(strings.ToLower(ct), "multipart/") {
		r, w := io.Pipe()
		o.pipe = w
		o.done = make(chan struct{})
		go func() {
			defer close(o.done)
			defer r.Close()
			defer func() {
				if recover() != nil {
					warn()
				}
			}()
			fields, files, truncated, undecoded := wire.ReadEncodedMultipart(r, ct, encoding, max)
			o.mu.Lock()
			o.fields = fields
			o.files = files
			o.capture.truncated = truncated
			o.undecodedEncoding = undecoded
			o.mu.Unlock()
		}()
	}
	return o
}
func (o *requestObserver) write(p []byte) {
	if o.pipe != nil {
		o.mu.Lock()
		if o.released {
			o.mu.Unlock()
			return
		}
		o.capture.size += int64(len(p))
		o.mu.Unlock()
		_, _ = o.pipe.Write(p)
		return
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	if !o.released {
		o.capture.write(p)
	}
}
func (o *requestObserver) release() {
	o.mu.Lock()
	o.released = true
	o.capture.data = nil
	o.fields = nil
	o.files = nil
	o.mu.Unlock()
}
func (o *requestObserver) finish() {
	o.once.Do(func() {
		if o.pipe != nil {
			_ = o.pipe.Close()
			<-o.done
		}
	})
}
func (o *requestObserver) snapshot(req *http.Request) wire.Request {
	o.mu.Lock()
	defer o.mu.Unlock()
	body := append([]byte(nil), o.capture.data...)
	if body == nil && (o.capture.truncated || o.complete && !o.capture.binary && o.pipe == nil) {
		body = []byte{} // Empty and fully observed is not an unavailable body.
	}
	return wire.Request{Method: req.Method, URL: req.URL.String(), Header: req.Header, Body: body, Fields: o.fields, Files: o.files, Size: o.capture.size, Truncated: o.capture.truncated, Unavailable: o.undecodedEncoding != "" || !o.complete && (req.ContentLength <= 0 || o.capture.size < req.ContentLength), MaxBodyBytes: o.capture.max, UndecodedEncoding: o.undecodedEncoding}
}

type requestBody struct {
	io.ReadCloser
	observer *requestObserver
}

func (b *requestBody) Read(p []byte) (int, error) {
	n, err := b.ReadCloser.Read(p)
	if n > 0 {
		b.observer.write(p[:n])
	}
	if err != nil {
		if err == io.EOF {
			b.observer.mu.Lock()
			b.observer.complete = true
			b.observer.mu.Unlock()
		}
		b.observer.finish()
	}
	return n, err
}
func (b *requestBody) Close() error { err := b.ReadCloser.Close(); b.observer.finish(); return err }
func (t *wireTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	start := time.Now() // Transport entry, before agent matching, copying or validation.
	if !t.state.active.Load() || req == nil || req.URL == nil {
		return t.base.RoundTrip(req)
	}
	// Observation is independent of span capture: every nested hook must see
	// headers added by auth transports, even under S4 or an active wire capture.
	// Suppressed calls inspect only headers/URL and never wrap/read the body.
	observeRequestCredentials(req)
	if t.skip(req.Context()) {
		return t.roundTripWithoutCapture(req)
	}
	m := wire.Find(req.Method, req.URL.String())
	if m == nil {
		return t.roundTripWithoutCapture(req)
	}
	parent := req.Context()
	flight := &captureFlight{}
	flight.active.Store(true)
	newAttempt := func() *requestObserver {
		return newObserver(req.Header.Get("Content-Type"), req.Header.Get("Content-Encoding"), t.state.config.WireMaxBodyBytes)
	}
	observer := newAttempt()
	// Every transport-requested replay is a separate observation. Reusing an
	// observer would concatenate retries and corrupt JSON/multipart capture.
	var attemptsMu sync.Mutex
	attempts := []*requestObserver{observer}
	requestsFinished := false
	finishRequest := func() wire.Request {
		flight.active.Store(false)
		attemptsMu.Lock()
		all := attempts
		requestsFinished = true
		attempts = nil
		attemptsMu.Unlock()
		latest := all[0]
		for _, attempt := range all {
			attempt.finish()
			attempt.mu.Lock()
			// Obtaining/closing a prospective replay is not a retry. Only
			// observed bytes or EOF can replace the authoritative upload.
			if attempt.capture.size > 0 || attempt.complete {
				latest = attempt
			}
			attempt.mu.Unlock()
		}
		request := latest.snapshot(req)
		for _, attempt := range all {
			// net/http responses also retain the outgoing Request, including its
			// Body/GetBody wrappers. Release capture there as well as in finalize.
			attempt.release()
		}
		return request
	}
	outgoing := req.Clone(context.WithValue(t.credentialContext(parent), captureKey, flight))
	// An upload reader may populate the original trailers as it reaches EOF.
	// Preserve that shared map: copying it changes the bytes net/http sends on
	// both HTTP/1.1 and HTTP/2. We never read or modify trailer values ourselves.
	outgoing.Trailer = req.Trailer
	// GetBody is arbitrary application code: never call it speculatively. Wrap
	// only a replay that the real transport itself chooses to obtain and read.
	if req.GetBody != nil {
		outgoing.GetBody = func() (io.ReadCloser, error) {
			body, err := req.GetBody()
			if err != nil || body == nil || body == http.NoBody {
				return body, err
			}
			attemptsMu.Lock()
			if requestsFinished {
				attemptsMu.Unlock()
				return body, err
			}
			next := newAttempt()
			attempts = append(attempts, next)
			attemptsMu.Unlock()
			return &requestBody{body, next}, nil
		}
	}
	if req.Body != nil && req.Body != http.NoBody {
		outgoing.Body = &requestBody{req.Body, observer}
	} else {
		observer.complete = true
		observer.finish()
	}
	untrack := t.track(req, outgoing)
	returned := false
	defer func() {
		if !returned {
			// Preserve an application panic verbatim, but retire every parser,
			// capture and cancellation mapping while unwinding. Never close an
			// application-owned body as part of instrumentation cleanup.
			untrack()
			_ = guarded(func() error { _ = finishRequest(); return nil })
		}
	}()
	resp, err := t.base.RoundTrip(outgoing)
	observed := time.Now() // Before upload finalization or response parsing.
	returned = true
	if resp != nil {
		observeRequestCredentials(resp.Request)
		if resp.Request == outgoing {
			resp.Request = req // Preserve the caller's Request identity and context.
		}
	}
	if err != nil {
		untrack()
		t.finish(parent, start, observed, m, finishRequest(), wire.Response{Error: err})
		return resp, err
	}
	if resp == nil {
		untrack()
		_ = finishRequest()
		return resp, err
	}
	if resp.StatusCode == http.StatusSwitchingProtocols {
		// Upgraded bodies are bidirectional streams, not an AI HTTP response.
		untrack()
		_ = finishRequest()
		return resp, err
	}
	response := wire.Response{Status: resp.StatusCode, Header: resp.Header}
	capture := bounded{max: t.state.config.WireMaxBodyBytes, binary: wire.Binary(wire.ContentType(resp.Header))}
	finalize := func(c responseCapture, readErr error, incomplete bool, end time.Time) {
		defer untrack()
		request := finishRequest()
		response.Body = c.data
		response.Size = c.size
		response.Truncated = c.truncated
		response.FirstChunkSeconds = c.first
		response.Arrivals = c.arrivals
		response.ArrivalsLost = c.arrivalsLost
		response.Error = readErr
		response.Incomplete = incomplete
		t.finish(parent, start, end, m, request, response)
	}
	if resp.Body == nil || resp.Body == http.NoBody {
		finalize(responseCapture{bounded: capture}, nil, false, observed)
	} else {
		last := time.Time{}
		if resp.ContentLength == 0 {
			last = observed
		}
		resp.Body = &responseBody{ReadCloser: resp.Body, capture: responseCapture{bounded: capture}, start: start, last: last, length: resp.ContentLength, complete: resp.ContentLength == 0, finalize: finalize}
	}
	return resp, nil
}

func (t *wireTransport) finish(ctx context.Context, start, end time.Time, m *wire.Match, req wire.Request, resp wire.Response) {
	defer func() {
		if recover() != nil {
			warn()
		}
	}()
	if !t.state.active.Load() {
		return
	}
	cfg := t.state.config
	req.ObserveSecret = RegisterSecret
	result := wire.Build(m, req, resp)
	if result == nil {
		return
	}
	values := attributes(result.Attributes, cfg)
	operation, _ := result.Attributes["gen_ai.operation.name"].(string)
	if extra, ok := ctx.Value(attributesKey).([]attribute.KeyValue); ok {
		var contextual []attribute.KeyValue
		for _, kv := range extra {
			switch kv.Value.Type() {
			case attribute.STRING:
				kv = attribute.String(string(kv.Key), captureString(kv.Value.AsString(), cfg))
			case attribute.STRINGSLICE:
				ss := append([]string(nil), kv.Value.AsStringSlice()...)
				for i := range ss {
					ss[i] = captureString(ss[i], cfg)
				}
				kv = attribute.StringSlice(string(kv.Key), ss)
			}
			if v, keep := cfg.TraceConfig.mask(kv, operation); keep {
				contextual = append(contextual, v)
			}
		}
		values = append(values, captureContext(contextual, values, cfg, operation)...)
	}
	values = budgetAttributes(values, t.state.attributeLimit)
	spanContext := context.WithoutCancel(ctx)
	_, span := t.state.tracer().Start(spanContext, captureString(result.Name, cfg), trace.WithTimestamp(start), trace.WithSpanKind(trace.SpanKindClient), trace.WithAttributes(values...))
	if result.OK {
		span.SetStatus(codes.Ok, "")
	} else {
		desc := result.Description
		if cfg.TraceConfig.HideInputs || cfg.TraceConfig.HideOutputs || cfg.TraceConfig.HideInputText || cfg.TraceConfig.HideOutputText {
			desc = ""
		}
		span.SetStatus(codes.Error, captureString(desc, cfg))
	}
	span.End(trace.WithTimestamp(end))
}

// responseCapture is the bounded body copy plus its transport observations:
// first non-empty chunk time and per-chunk arrival times (A10), recorded when
// each Read returns and before any copying, decoding or parsing.
type responseCapture struct {
	bounded
	first        *float64
	arrivals     []wire.Arrival
	arrivalsLost bool
}

func (c *responseCapture) observe(p []byte, seconds float64) {
	if c.first == nil {
		first := seconds
		c.first = &first
	}
	c.write(p)
	if c.binary || c.truncated {
		c.arrivals = nil // Unparsed bodies need no frame association.
		return
	}
	if len(c.arrivals) >= wire.MaxArrivals {
		c.arrivalsLost = true
		return
	}
	c.arrivals = append(c.arrivals, wire.Arrival{End: c.size, Seconds: seconds})
}

// captureContext scrubs ALL context attributes as ONE object, so a MIME/type
// sibling reaches its payload (headerless audio has no magic bytes), with the
// wire span's own hint keys as siblings. Per-value scrubbing cannot see that
// context. Protected identity (user.id, session.id, ...) is walked jointly too;
// when the whole object is an image block, the walk itself keeps protected keys
// in their scrubbed form. No path ever restores an unchecked input value: the
// export sanitizer cannot recover MIME context that capture discarded.
//
// On a privacy-budget failure it fails closed: content context is dropped and
// a degradation marker is added. Protected identity is kept only if a second
// walk (protected keys plus EVERY MIME/type hint of the context, fresh budget)
// checks it; then scalar protected keys alone with those hints; else nothing.
func captureContext(extra, wireValues []attribute.KeyValue, cfg Config, operation string) (out []attribute.KeyValue) {
	if len(extra) == 0 {
		return nil
	}
	degraded := attribute.Bool("traceai.wire.privacy_degraded", true)
	defer func() {
		if recover() != nil {
			warn()
			out = []attribute.KeyValue{degraded}
		}
	}()
	siblings := map[string]any{}
	for _, kv := range wireValues {
		if _, ok := mediaHintParent(kv); ok {
			siblings[string(kv.Key)], _ = hintValue(kv)
		}
	}
	if out, ok := scrubContext(extra, siblings, cfg, operation); ok {
		return out
	}
	var protected, scalar, hints []attribute.KeyValue
	names := map[attribute.Key]bool{}
	for _, kv := range extra {
		if protectedAttributePriority(kv) >= 0 {
			protected = append(protected, kv)
			names[kv.Key] = true
			switch kv.Value.Type() {
			case attribute.STRING, attribute.INT64, attribute.FLOAT64, attribute.BOOL:
				scalar = append(scalar, kv)
			}
		} else if _, ok := mediaHintParent(kv); ok {
			hints = append(hints, kv)
		}
	}
	for i, subset := range [][]attribute.KeyValue{protected, scalar} {
		if len(subset) == 0 || i > 0 && len(subset) == len(protected) {
			break // Nothing to keep, or the scalar retry would repeat tier two.
		}
		if checked, ok := scrubContext(append(append([]attribute.KeyValue(nil), subset...), hints...), siblings, cfg, operation); ok {
			out = make([]attribute.KeyValue, 0, len(subset)+1)
			for _, kv := range checked {
				if names[kv.Key] {
					out = append(out, kv)
				}
			}
			return append(out, degraded)
		}
	}
	return []attribute.KeyValue{degraded}
}

// scrubContext is one bounded joint walk; ok=false on a privacy-budget failure.
func scrubContext(attrs []attribute.KeyValue, siblings map[string]any, cfg Config, operation string) (out []attribute.KeyValue, ok bool) {
	defer func() {
		if recover() != nil {
			warn()
			out, ok = nil, false
		}
	}()
	s := &spanSanitizer{config: cfg, operation: operation, secrets: noSecrets, nodes: 16384, bytes: 32 * 1024 * 1024, siblings: siblings}
	return s.attrs(attrs, false, 0, 0), true
}

type responseBody struct {
	io.ReadCloser
	mu       sync.Mutex
	capture  responseCapture
	start    time.Time
	last     time.Time
	once     sync.Once
	closing  bool
	complete bool
	length   int64
	finalize func(responseCapture, error, bool, time.Time)
}

func (b *responseBody) Read(p []byte) (int, error) {
	// Preserve an application-owned reader's panic, but classify the ensuing
	// abort as consumer cancellation rather than inventing a transport error.
	defer func() {
		if v := recover(); v != nil {
			b.end(nil, true, time.Now())
			panic(v)
		}
	}()
	n, err := b.ReadCloser.Read(p)
	observed := time.Now() // Last byte/error observed, before copying or parsing.
	closing, complete := false, false
	// Only the copy is synchronized. Never hold a lock across a host body Read
	// or Close: net/http permits Close to interrupt a blocked Read.
	func() {
		defer func() {
			if recover() != nil {
				warn()
			}
		}()
		b.mu.Lock()
		defer b.mu.Unlock()
		closing = b.closing
		if b.finalize == nil {
			return // Never retain bytes from reads after capture has completed.
		}
		if n > 0 {
			b.last = observed
			if b.length == 0 {
				b.complete = false
			} // Tolerate a nonconforming custom response length.
			b.capture.observe(p[:n], observed.Sub(b.start).Seconds())
		}
		if err == io.EOF || b.length > 0 && b.capture.size >= b.length {
			b.complete = true
		}
		complete = b.complete
	}()
	if err == io.EOF {
		b.end(nil, false, observed)
	} else if err != nil {
		if closing {
			b.end(nil, true, observed)
		} else {
			b.end(err, false, observed)
		}
	} else if complete && n > 0 {
		b.end(nil, false, observed)
	}
	return n, err
}
func (b *responseBody) Close() error {
	b.mu.Lock()
	b.closing = true
	b.mu.Unlock()
	defer b.end(nil, true, time.Now()) // Consumer close (including a panic) is not a transport failure.
	return b.ReadCloser.Close()
}
func (b *responseBody) end(err error, incomplete bool, observed time.Time) {
	b.once.Do(func() {
		defer func() {
			if recover() != nil {
				warn()
			}
		}()
		b.mu.Lock()
		c, finalize := b.capture, b.finalize
		incomplete = incomplete && !b.complete
		// EOF/Close may follow the final successful read much later. Consumer
		// think time and a second empty EOF read are not transport duration.
		if err == nil && !b.last.IsZero() {
			observed = b.last
		}
		b.capture = responseCapture{}
		// This closure owns every upload/replay observer. A completed response
		// kept for headers or status must not keep those captured bytes alive.
		b.finalize = nil
		b.mu.Unlock()
		finalize(c, err, incomplete, observed)
	})
}
