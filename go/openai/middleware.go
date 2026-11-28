package traceaiopenai

import (
	"bytes"
	"context"
	"errors"
	"io"
	"mime"
	"net/http"
	"strconv"
	"strings"

	"github.com/openai/openai-go/v3/option"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

const (
	// Version is the version of this module.
	Version = "0.1.0"
	// ScopeName is the instrumentation scope of the spans this package starts.
	ScopeName = "github.com/future-agi/traceAI/go/openai"

	maxRequestBody  = 32 << 20
	maxErrorBody    = 1 << 20
	maxErrorMessage = 1024
	retryHeader     = "X-Stainless-Retry-Count"
)

type config struct {
	tp    trace.TracerProvider
	tc    TraceConfig
	tcSet bool
}

// Option configures [Middleware].
type Option func(*config)

// WithTracerProvider sets the provider spans are started on. Without it the
// OpenTelemetry global provider (otel.GetTracerProvider) is used.
func WithTracerProvider(tp trace.TracerProvider) Option {
	return func(c *config) { c.tp = tp }
}

// WithTraceConfig sets the privacy switches. Without it they are read once
// from the FI_HIDE_* environment variables when [Middleware] is called.
func WithTraceConfig(tc TraceConfig) Option {
	return func(c *config) { c.tc, c.tcSet = tc, true }
}

// Middleware returns the tracing middleware. Pass it to
// option.WithMiddleware when you build the client, or per request.
//
// Chat Completions, Responses creation and Embeddings requests are spanned.
// Other requests pass through untouched.
func Middleware(opts ...Option) option.Middleware {
	c := &config{}
	for _, o := range opts {
		o(c)
	}
	if !c.tcSet {
		c.tc = TraceConfigFromEnv()
	}
	return func(req *http.Request, next option.MiddlewareNext) (*http.Response, error) {
		op := operationForRequest(req)
		if op == "" {
			return next(req)
		}
		return c.traceRequest(req, next, op)
	}
}

func (c *config) tracer() trace.Tracer {
	tp := c.tp
	if tp == nil {
		tp = otel.GetTracerProvider()
	}
	return tp.Tracer(ScopeName, trace.WithInstrumentationVersion(Version))
}

func (c *config) traceRequest(req *http.Request, next option.MiddlewareNext, op operation) (*http.Response, error) {
	parsed := parseRequest(readRequestBody(req), c.tc, op)
	kind := SpanKindLLM
	if op == opEmbeddings {
		kind = SpanKindEmbedding
	}
	attrs := []attribute.KeyValue{
		attribute.String(GenAISpanKind, kind),
		attribute.String(GenAIOperationName, string(op)),
		attribute.String(GenAIProviderName, providerFromHost(req.URL.Hostname())),
	}
	attrs = append(attrs, parsed.attrs...)
	if n, err := strconv.Atoi(req.Header.Get(retryHeader)); err == nil && n > 0 {
		attrs = append(attrs, attribute.Int(HTTPRequestResendCount, n))
	}
	attrs = append(attrs, ContextAttributes(req.Context())...)

	name := string(op)
	if parsed.model != "" {
		name += " " + parsed.model
	}
	reqCtx := req.Context()
	ctx, span := c.tracer().Start(reqCtx, name,
		trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(c.tc.Filter(attrs)...),
	)

	res, err := next(req.WithContext(ctx))
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
		c.endHTTPError(span, res)
	case isEventStream(res):
		res.Body = newStreamBody(reqCtx, res.Body, span, c.tc, op)
	default:
		c.endJSON(reqCtx, span, res, op)
	}
	return res, nil
}

// readRequestBody returns a copy of the JSON request body without consuming
// it for the SDK. It returns nil when the body is absent or too large.
func readRequestBody(req *http.Request) []byte {
	if req.Body == nil || req.Body == http.NoBody {
		return nil
	}
	if req.GetBody != nil {
		rc, err := req.GetBody()
		if err != nil {
			return nil
		}
		defer rc.Close()
		b, err := io.ReadAll(io.LimitReader(rc, maxRequestBody+1))
		if err != nil || len(b) > maxRequestBody {
			return nil
		}
		return b
	}
	orig := req.Body
	b, err := io.ReadAll(io.LimitReader(orig, maxRequestBody+1))
	req.Body = readCloser{io.MultiReader(bytes.NewReader(b), orig), orig}
	if err != nil || len(b) > maxRequestBody {
		return nil
	}
	return b
}

type readCloser struct {
	io.Reader
	io.Closer
}

type errReader struct{ err error }

func (e errReader) Read([]byte) (int, error) { return 0, e.err }

func isEventStream(res *http.Response) bool {
	mt, _, _ := mime.ParseMediaType(res.Header.Get("Content-Type"))
	return mt == "text/event-stream"
}

func isJSON(res *http.Response) bool {
	ct := res.Header.Get("Content-Type")
	if ct == "" {
		return true
	}
	mt, _, _ := mime.ParseMediaType(ct)
	return strings.Contains(mt, "json")
}

// endJSON reads a non-streaming response, restores the body for the SDK and
// ends the span with the response attributes.
func (c *config) endJSON(reqCtx context.Context, span trace.Span, res *http.Response, op operation) {
	if res.Body == nil || !isJSON(res) {
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
	acc.addJSON(b, op)
	span.SetAttributes(c.tc.Filter(acc.attributes(c.tc))...)
	setResponseError(span, acc.streamErr, c.tc, op)
	span.End()
}

// endHTTPError ends the span for a 4xx/5xx response. The error type and code
// are kept; the API's error message only while inputs and outputs are both
// captured.
func (c *config) endHTTPError(span trace.Span, res *http.Response) {
	var b []byte
	if res.Body != nil {
		orig := res.Body
		b, _ = io.ReadAll(io.LimitReader(orig, maxErrorBody))
		res.Body = readCloser{io.MultiReader(bytes.NewReader(b), orig), orig}
	}
	e := parseAPIError(b)
	errType := strconv.Itoa(res.StatusCode)
	if safeToken(e.Type) {
		errType = e.Type
	}
	desc := "HTTP " + strconv.Itoa(res.StatusCode)
	if safeToken(e.Code) {
		desc += " " + e.Code
	}
	attrs := []attribute.KeyValue{attribute.String(ErrorType, errType)}
	if msg := truncate(e.Message, maxErrorMessage); msg != "" {
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

// safeToken reports whether s is a short machine token such as
// "invalid_request_error" or "model_not_found", safe to keep even when
// content capture is off.
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

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}

// providerFromHost labels Azure OpenAI resources "azure" and everything else,
// including OpenAI-compatible proxies, "openai", like traceAI-openai for
// Python.
func providerFromHost(host string) string {
	h := strings.ToLower(strings.TrimSuffix(host, "."))
	for _, s := range []string{".openai.azure.com", ".cognitiveservices.azure.com", ".services.ai.azure.com"} {
		if strings.HasSuffix(h, s) {
			return "azure"
		}
	}
	return "openai"
}
