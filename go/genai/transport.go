package traceaigenai

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

const (
	// Version is the version of this module.
	Version = "0.1.0"
	// ScopeName is the instrumentation scope of the spans this package starts.
	ScopeName = "github.com/future-agi/traceAI/go/genai"

	maxRequestBody  = 32 << 20
	maxErrorBody    = 1 << 20
	maxErrorMessage = 1024

	methodGenerate       = ":generateContent"
	methodStreamGenerate = ":streamGenerateContent"
	apiKeyHeader         = "x-goog-api-key"
)

type config struct {
	tp    trace.TracerProvider
	tc    TraceConfig
	tcSet bool
}

// Option configures [NewTransport], [NewHTTPClient] and [WrapClient].
type Option func(*config)

// WithTracerProvider sets the provider spans are started on. Without it the
// OpenTelemetry global provider (otel.GetTracerProvider) is used at the time
// of each request.
func WithTracerProvider(tp trace.TracerProvider) Option {
	return func(c *config) { c.tp = tp }
}

// WithTraceConfig sets the privacy switches. Without it they are read once
// from the FI_HIDE_* environment variables when the transport is built.
func WithTraceConfig(tc TraceConfig) Option {
	return func(c *config) { c.tc, c.tcSet = tc, true }
}

// Transport is an http.RoundTripper that traces Gemini generate calls made by
// google.golang.org/genai. Build it with [NewTransport].
type Transport struct {
	base http.RoundTripper
	cfg  *config
}

// NewTransport returns a RoundTripper that sends every request through base
// (http.DefaultTransport when nil) and spans the GenerateContent and
// GenerateContentStream calls among them. Any other request (files, caches,
// tokens, embeddings, models, batches, interactions) passes through untouched
// and without a span.
func NewTransport(base http.RoundTripper, opts ...Option) *Transport {
	c := &config{}
	for _, o := range opts {
		o(c)
	}
	if !c.tcSet {
		c.tc = TraceConfigFromEnv()
	}
	if base == nil {
		base = http.DefaultTransport
	}
	return &Transport{base: base, cfg: c}
}

// NewHTTPClient returns an *http.Client for genai.ClientConfig.HTTPClient. It
// behaves like the client genai builds when HTTPClient is nil (the default
// transport, no client timeout), plus tracing:
//
//	client, err := genai.NewClient(ctx, &genai.ClientConfig{
//		Backend:    genai.BackendGeminiAPI,
//		HTTPClient: traceaigenai.NewHTTPClient(traceaigenai.WithTracerProvider(tp)),
//	})
func NewHTTPClient(opts ...Option) *http.Client {
	return &http.Client{Transport: NewTransport(nil, opts...)}
}

// WrapClient returns a copy of hc whose transport is traced. hc itself is not
// changed. Use it when you already build your own *http.Client (a proxy, a
// custom timeout). A nil hc is treated as an empty client.
func WrapClient(hc *http.Client, opts ...Option) *http.Client {
	var out http.Client
	if hc != nil {
		out = *hc
	}
	out.Transport = NewTransport(out.Transport, opts...)
	return &out
}

// RoundTrip implements http.RoundTripper.
func (t *Transport) RoundTrip(req *http.Request) (*http.Response, error) {
	method, ok := generateMethod(req)
	if !ok {
		return t.base.RoundTrip(req)
	}
	return t.traceGenerate(req, method == methodStreamGenerate)
}

// generateMethod reports whether req is a Gemini generate call and which one.
// The SDK builds {base}/{version}/models/{model}:generateContent and
// :streamGenerateContent?alt=sse for both backends.
func generateMethod(req *http.Request) (string, bool) {
	if req == nil || req.URL == nil || req.Method != http.MethodPost {
		return "", false
	}
	p := strings.TrimRight(req.URL.Path, "/")
	switch {
	case strings.HasSuffix(p, methodGenerate):
		return methodGenerate, true
	case strings.HasSuffix(p, methodStreamGenerate):
		return methodStreamGenerate, true
	}
	return "", false
}

// modelFromPath returns the model id from a generate URL path:
// /v1beta/models/gemini-x:generateContent gives "gemini-x",
// .../tunedModels/t:generateContent gives "tunedModels/t", and a Vertex
// publisher path gives the model after /models/.
func modelFromPath(path string) string {
	p := strings.TrimRight(path, "/")
	if i := strings.LastIndexByte(p, ':'); i >= 0 {
		p = p[:i]
	}
	if i := strings.LastIndex(p, "/models/"); i >= 0 {
		return p[i+len("/models/"):]
	}
	if i := strings.LastIndex(p, "/tunedModels/"); i >= 0 {
		return p[i+1:]
	}
	if i := strings.LastIndexByte(p, '/'); i >= 0 {
		return p[i+1:]
	}
	return p
}

func (c *config) tracer() trace.Tracer {
	tp := c.tp
	if tp == nil {
		tp = otel.GetTracerProvider()
	}
	return tp.Tracer(ScopeName, trace.WithInstrumentationVersion(Version))
}

func (t *Transport) traceGenerate(req *http.Request, streaming bool) (*http.Response, error) {
	c := t.cfg
	body, out := requestBody(req)
	model := modelFromPath(req.URL.Path)
	parsed := parseGenerateRequest(body, c.tc)

	attrs := []attribute.KeyValue{
		attribute.String(GenAISpanKind, SpanKindLLM),
		attribute.String(GenAIOperationName, OperationGenerateContent),
		attribute.String(GenAIProviderName, ProviderGoogle),
	}
	if model != "" {
		attrs = append(attrs, attribute.String(GenAIRequestModel, model))
	}
	attrs = append(attrs, parsed.attrs...)
	attrs = append(attrs, ContextAttributes(req.Context())...)

	name := OperationGenerateContent
	if model != "" {
		name += " " + model
	}
	reqCtx := req.Context()
	ctx, span := c.tracer().Start(reqCtx, name,
		trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(c.tc.Filter(attrs)...),
	)
	// The request goes on with the span in its context, so an instrumented
	// base transport nests its HTTP span under this one. Nothing else about
	// the request changes.
	out = out.WithContext(ctx)

	secrets := requestSecrets(req)
	res, err := t.base.RoundTrip(out)
	if err != nil {
		endWithFailure(span, failureClass(reqCtx, err))
		return res, err
	}
	if res == nil {
		span.End()
		return res, err
	}
	span.SetAttributes(attribute.Int(HTTPResponseStatusCode, res.StatusCode))
	switch {
	case res.StatusCode >= 400:
		c.endHTTPError(span, res, secrets)
	case streaming:
		res.Body = newStreamBody(reqCtx, res.Body, span, c.tc, secrets)
	default:
		c.endJSON(reqCtx, span, res)
	}
	return res, nil
}

// requestBody returns a copy of the JSON request body and the request to send.
// The SDK builds its requests with a replayable body (GetBody), so the copy is
// read from GetBody and req is sent unchanged. Without GetBody the body is
// read once and a clone of req carries a replay of it; req itself is never
// modified (http.RoundTripper contract).
func requestBody(req *http.Request) ([]byte, *http.Request) {
	if req.Body == nil || req.Body == http.NoBody {
		return nil, req
	}
	if req.GetBody != nil {
		rc, err := req.GetBody()
		if err != nil {
			return nil, req
		}
		defer rc.Close()
		b, err := io.ReadAll(io.LimitReader(rc, maxRequestBody+1))
		if err != nil || len(b) > maxRequestBody {
			return nil, req
		}
		return b, req
	}
	orig := req.Body
	b, err := io.ReadAll(io.LimitReader(orig, maxRequestBody+1))
	out := req.Clone(req.Context())
	out.Body = readCloser{io.MultiReader(bytes.NewReader(b), orig), orig}
	if err != nil || len(b) > maxRequestBody {
		return nil, out
	}
	return b, out
}

// requestSecrets returns credential values present on the request, so they
// can be scrubbed from any API error text before it is written to a span.
func requestSecrets(req *http.Request) []string {
	var s []string
	if v := strings.TrimSpace(req.Header.Get(apiKeyHeader)); len(v) >= 8 {
		s = append(s, v)
	}
	if v := req.Header.Get("Authorization"); v != "" {
		if _, tok, ok := strings.Cut(v, " "); ok && len(tok) >= 8 {
			s = append(s, tok)
		}
	}
	if v := req.URL.Query().Get("key"); len(v) >= 8 {
		s = append(s, v)
	}
	return s
}

type readCloser struct {
	io.Reader
	io.Closer
}

type errReader struct{ err error }

func (e errReader) Read([]byte) (int, error) { return 0, e.err }

// endJSON reads a non-streaming response, restores the body for the SDK and
// ends the span with the response attributes.
func (c *config) endJSON(reqCtx context.Context, span trace.Span, res *http.Response) {
	if res.Body == nil {
		span.End()
		return
	}
	orig := res.Body
	b, err := io.ReadAll(orig)
	_ = orig.Close()
	if err != nil {
		res.Body = io.NopCloser(io.MultiReader(bytes.NewReader(b), errReader{err}))
		endWithFailure(span, failureClass(reqCtx, err))
		return
	}
	res.Body = io.NopCloser(bytes.NewReader(b))
	acc := newAccumulator()
	acc.addResponse(b)
	span.SetAttributes(c.tc.Filter(acc.attributes())...)
	span.End()
}

// endHTTPError ends the span for a 4xx/5xx response. The status token
// (NOT_FOUND, INVALID_ARGUMENT, ...) is kept; the API's error message only
// while inputs and outputs are both captured, and never with a credential in
// it.
func (c *config) endHTTPError(span trace.Span, res *http.Response, secrets []string) {
	var b []byte
	if res.Body != nil {
		orig := res.Body
		b, _ = io.ReadAll(io.LimitReader(orig, maxErrorBody))
		res.Body = readCloser{io.MultiReader(bytes.NewReader(b), orig), orig}
	}
	e := parseAPIError(b)
	desc := "HTTP " + strconv.Itoa(res.StatusCode)
	errType := strconv.Itoa(res.StatusCode)
	if safeToken(e.Status) {
		errType = e.Status
		desc += " " + e.Status
	}
	c.endAPIError(span, errType, desc, e.Message, secrets)
}

func (c *config) endAPIError(span trace.Span, errType, desc, message string, secrets []string) {
	attrs := []attribute.KeyValue{attribute.String(ErrorType, errType)}
	if msg := truncate(scrub(message, secrets), maxErrorMessage); msg != "" {
		attrs = append(attrs, attribute.String(ErrorMessage, msg))
		if c.tc.errorMessageAllowed() {
			desc += ": " + msg
		}
	}
	span.SetAttributes(c.tc.Filter(attrs)...)
	span.SetStatus(codes.Error, desc)
	span.End()
}

type failure struct{ errType, desc string }

// failureClass describes a transport or read error without its text, which
// carries URLs and host names.
func failureClass(reqCtx context.Context, err error) failure {
	switch {
	case errors.Is(err, context.Canceled) || errors.Is(reqCtx.Err(), context.Canceled):
		return failure{"canceled", "request canceled before the response completed"}
	case errors.Is(err, context.DeadlineExceeded) || errors.Is(reqCtx.Err(), context.DeadlineExceeded):
		return failure{"timeout", "request timed out before the response completed"}
	default:
		return failure{"transport", "transport error before the response completed"}
	}
}

func endWithFailure(span trace.Span, f failure) {
	span.SetAttributes(attribute.String(ErrorType, f.errType))
	span.SetStatus(codes.Error, f.desc)
	span.End()
}

// safeToken reports whether s is a short machine token such as "NOT_FOUND" or
// "RESOURCE_EXHAUSTED", safe to keep even when content capture is off.
func safeToken(s string) bool {
	if s == "" || len(s) > 64 {
		return false
	}
	for _, r := range s {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '_' || r == '-' || r == '.') {
			return false
		}
	}
	return true
}

func scrub(s string, secrets []string) string {
	for _, v := range secrets {
		if v != "" {
			s = strings.ReplaceAll(s, v, RedactedValue)
		}
	}
	return s
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}
