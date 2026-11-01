package traceaianthropic

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"mime"
	"net/http"
	"os"
	"strconv"
	"strings"

	"github.com/anthropics/anthropic-sdk-go/option"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

// InstrumentationName is the instrumentation scope of the spans that this
// package opens itself.
const InstrumentationName = "github.com/future-agi/traceAI/go/anthropic"

// Option configures [Middleware].
type Option func(*settings)

type settings struct {
	provider trace.TracerProvider
	config   *TraceConfig
}

// WithTracerProvider sets the tracer provider of the spans that the middleware
// opens itself. Without it, the middleware uses the provider registered with
// otel.SetTracerProvider at the time of each call. It has no effect on the
// span that anthropic-sdk-go opens: pass the same provider to the SDK with
// option.WithTracerProvider, or register it globally, which covers both.
func WithTracerProvider(provider trace.TracerProvider) Option {
	return func(s *settings) { s.provider = provider }
}

// WithTraceConfig sets what the spans capture. Without it, the middleware reads
// the FI_HIDE_* environment variables once, when it is created
// ([TraceConfigFromEnv]).
func WithTraceConfig(config TraceConfig) Option {
	return func(s *settings) { s.config = &config }
}

// ClientOption returns the anthropic-sdk-go client option that installs the
// middleware: option.WithMiddleware(Middleware(opts...)).
func ClientOption(opts ...Option) option.RequestOption {
	return option.WithMiddleware(Middleware(opts...))
}

// Middleware returns an anthropic-sdk-go middleware that traces every Messages
// call of the client it is passed to, streaming or not:
//
//	client := anthropic.NewClient(option.WithMiddleware(traceaianthropic.Middleware()))
//
// From anthropic-sdk-go v1.81.0 the SDK opens a span for each API call itself
// ("anthropic.messages.create"). When that span is recording, the middleware
// adds the Future AGI attributes to it, so each call is still one span. When
// it is not (spans turned off with option.WithoutOpenTelemetry or
// ANTHROPIC_OPEN_TELEMETRY=false, or an SDK older than v1.81.0), the middleware
// opens a span of its own, with the same name and the same gen_ai.* keys, for
// each HTTP attempt.
func Middleware(opts ...Option) option.Middleware {
	s := settings{}
	for _, o := range opts {
		o(&s)
	}
	cfg := TraceConfigFromEnv()
	if s.config != nil {
		cfg = *s.config
	}
	warnSDKContentMode(cfg)
	in := &instrumentor{provider: s.provider, cfg: cfg}
	return in.handle
}

// sdkContentModeEnv is the anthropic-sdk-go variable that makes the SDK's own
// span carry request and response content.
const sdkContentModeEnv = "ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE"

// warnSDKContentMode logs when a FI_HIDE_* setting asks to leave content out
// while anthropic-sdk-go is told to put content on its span: the SDK does not
// read the FI_HIDE_* variables, so its content would still be exported.
func warnSDKContentMode(cfg TraceConfig) {
	if !strings.EqualFold(os.Getenv(sdkContentModeEnv), "content") {
		return
	}
	if cfg.HideInputs || cfg.HideOutputs || cfg.HideInputMessages || cfg.HideOutputMessages || cfg.HideInputText || cfg.HideOutputText {
		log.Printf("traceaianthropic: %s=content makes anthropic-sdk-go put request and response content on its spans, which FI_HIDE_* settings do not remove; unset it to keep content out", sdkContentModeEnv)
	}
}

type instrumentor struct {
	provider trace.TracerProvider
	cfg      TraceConfig
}

func (in *instrumentor) tracer() trace.Tracer {
	p := in.provider
	if p == nil {
		p = otel.GetTracerProvider()
	}
	return p.Tracer(InstrumentationName, trace.WithInstrumentationVersion(Version))
}

// namedSpan is implemented by the spans of the OpenTelemetry SDK. The
// middleware uses it to tell anthropic-sdk-go's own Messages span from a span
// of the application.
type namedSpan interface{ Name() string }

func sdkMessagesSpan(ctx context.Context) (trace.Span, bool) {
	span := trace.SpanFromContext(ctx)
	if !span.IsRecording() {
		return nil, false
	}
	n, ok := span.(namedSpan)
	if !ok || n.Name() != sdkSpanName {
		return nil, false
	}
	return span, true
}

// isMessagesRequest reports whether req creates a message on the Messages API.
func isMessagesRequest(req *http.Request) bool {
	return req.Method == http.MethodPost && req.URL != nil && strings.HasSuffix(strings.TrimRight(req.URL.Path, "/"), "/v1/messages")
}

// call is the state of one traced request.
type call struct {
	in   *instrumentor
	span trace.Span
	// own is true when the middleware opened span, and so sets the gen_ai.*
	// keys that the SDK would otherwise set, and ends it.
	own bool
}

func (in *instrumentor) handle(req *http.Request, next option.MiddlewareNext) (*http.Response, error) {
	ctx := req.Context()
	sdkSpan, enrich := sdkMessagesSpan(ctx)
	if !enrich && !isMessagesRequest(req) {
		return next(req)
	}
	body, ok := readRequestBody(req)
	if !ok {
		return next(req)
	}
	var parsed messagesRequest
	_ = json.Unmarshal(body, &parsed)

	c := &call{in: in, own: !enrich}
	if enrich {
		c.span = sdkSpan
	} else {
		ctx, c.span = in.tracer().Start(ctx, sdkSpanName,
			trace.WithSpanKind(trace.SpanKindClient),
			trace.WithAttributes(
				keyOperationName.String(operationChat),
				keyProviderName.String(providerName),
				keyRequestModel.String(parsed.Model),
			))
		req = req.WithContext(ctx)
	}
	c.recordRequest(ctx, req, body, &parsed)

	res, err := next(req)
	if err != nil {
		c.fail(errorType(err))
		return res, err
	}
	if c.own {
		c.span.SetAttributes(keyHTTPStatusCode.Int(res.StatusCode))
	}
	if res.StatusCode >= 400 {
		c.recordAPIError(res)
		return res, nil
	}
	switch mediaType(res) {
	case "text/event-stream":
		res.Body = newStreamBody(ctx, res.Body, c)
	case "application/json":
		data, rerr := readResponseBody(res)
		if rerr != nil {
			c.fail(errorType(rerr))
			return res, nil
		}
		var m message
		if json.Unmarshal(data, &m) == nil {
			c.recordMessage(&m, string(data))
		}
		c.end()
	default:
		// A body this package does not decode, such as the binary event stream
		// of Amazon Bedrock, goes to the application untouched and unbuffered.
		c.end()
	}
	return res, nil
}

func mediaType(res *http.Response) string {
	mt, _, err := mime.ParseMediaType(res.Header.Get("Content-Type"))
	if err != nil {
		return ""
	}
	return strings.ToLower(mt)
}

// readRequestBody returns the request body and leaves req with a body that
// sends the same bytes.
func readRequestBody(req *http.Request) ([]byte, bool) {
	if req.Body == nil || req.Body == http.NoBody {
		return nil, false
	}
	if req.GetBody != nil {
		rc, err := req.GetBody()
		if err != nil {
			return nil, false
		}
		defer rc.Close()
		b, err := io.ReadAll(rc)
		return b, err == nil
	}
	b, err := io.ReadAll(req.Body)
	req.Body.Close()
	if err != nil {
		req.Body = io.NopCloser(io.MultiReader(bytes.NewReader(b), errReader{err}))
		return nil, false
	}
	req.Body = io.NopCloser(bytes.NewReader(b))
	req.GetBody = func() (io.ReadCloser, error) { return io.NopCloser(bytes.NewReader(b)), nil }
	return b, true
}

// readResponseBody reads the whole body of res and leaves res with a body that
// returns the same bytes, followed by the read error if there was one.
func readResponseBody(res *http.Response) ([]byte, error) {
	if res.Body == nil || res.Body == http.NoBody {
		return nil, nil
	}
	b, err := io.ReadAll(res.Body)
	res.Body.Close()
	if err != nil {
		res.Body = io.NopCloser(io.MultiReader(bytes.NewReader(b), errReader{err}))
		return b, err
	}
	res.Body = io.NopCloser(bytes.NewReader(b))
	return b, nil
}

type errReader struct{ err error }

func (r errReader) Read([]byte) (int, error) { return 0, r.err }

func (c *call) set(kv ...attribute.KeyValue) { c.span.SetAttributes(kv...) }

func (c *call) recordRequest(ctx context.Context, req *http.Request, body []byte, r *messagesRequest) {
	cfg := c.in.cfg
	c.set(keySpanKind.String(spanKindLLM))
	if c.own {
		if r.MaxTokens != nil {
			c.set(keyRequestMaxTokens.Int64(*r.MaxTokens))
		}
		if r.Temperature != nil {
			c.set(keyRequestTemperature.Float64(*r.Temperature))
		}
		if r.TopP != nil {
			c.set(keyRequestTopP.Float64(*r.TopP))
		}
		if r.TopK != nil {
			c.set(keyRequestTopK.Int64(*r.TopK))
		}
		if r.Stream {
			c.set(keyRequestStream.Bool(true))
		}
		if !cfg.HideInputs && len(r.StopSequences) > 0 {
			c.set(keyRequestStopSequences.StringSlice(r.StopSequences))
		}
		// The SDK numbers its attempts in this header; a retry is attempt 1 or later.
		if n, err := strconv.Atoi(req.Header.Get("X-Stainless-Retry-Count")); err == nil && n > 0 {
			c.set(keyHTTPResendCount.Int(n))
		}
	}
	if !cfg.HideLLMInvocationParameters {
		var top map[string]json.RawMessage
		if json.Unmarshal(body, &top) == nil {
			if v, ok := invocationParameters(top); ok {
				c.set(keyRequestParameters.String(v))
			}
		}
	}
	if cfg.HideInputs {
		c.set(keyInputValue.String(redactedValue))
	} else {
		v, cut := capValue(sanitizeInputValue(body, cfg))
		c.set(keyInputValue.String(v), keyInputMimeType.String(mimeJSON))
		if cut {
			c.set(keyAnthropicTruncated.Bool(true))
		}
		if tools := bytes.TrimSpace(r.Tools); len(tools) > 0 && !bytes.Equal(tools, []byte("null")) {
			c.set(keyToolDefinitions.String(string(tools)))
		}
	}
	if !cfg.inputMessagesHidden() {
		if s, ok := marshalString(inputMessages(r, cfg)); ok {
			c.set(keyInputMessages.String(s))
		}
		if sys := systemInstructions(r.System, cfg); len(sys) > 0 {
			if s, ok := marshalString(sys); ok {
				c.set(keySystemInstructions.String(s))
			}
		}
	}
	if id, ok := SessionIDFromContext(ctx); ok {
		c.set(keySessionID.String(id))
	}
	if id, ok := UserIDFromContext(ctx); ok {
		c.set(keyUserID.String(id))
	}
}

// recordMessage records a response message. raw is output.value: the response
// body, or the message that a stream assembled.
func (c *call) recordMessage(m *message, raw string) {
	cfg := c.in.cfg
	if m.StopReason != nil && *m.StopReason != "" {
		c.set(keyResponseFinishReasons.StringSlice([]string{*m.StopReason}))
	}
	if c.own {
		if m.ID != "" {
			c.set(keyResponseID.String(m.ID))
		}
		if m.Model != "" {
			c.set(keyResponseModel.String(m.Model))
		}
		if m.StopReason != nil && *m.StopReason != "" {
			c.set(keyAnthropicStop.String(*m.StopReason))
		}
		c.set(usageAttributes(m.Usage)...)
	}
	if cfg.HideOutputs {
		c.set(keyOutputValue.String(redactedValue))
	} else {
		v, cut := capValue(raw)
		c.set(keyOutputValue.String(v), keyOutputMimeType.String(mimeJSON))
		if cut {
			c.set(keyAnthropicTruncated.Bool(true))
		}
	}
	if !cfg.outputMessagesHidden() {
		if s, ok := marshalString(outputMessages(m, cfg)); ok {
			c.set(keyOutputMessages.String(s))
		}
	}
}

// usageAttributes returns the token counts of u. As in the OpenTelemetry GenAI
// conventions, and as anthropic-sdk-go's own spans do, gen_ai.usage.input_tokens
// is every input token, cached or not: the API's input_tokens plus its cache
// read and cache creation tokens. The two cache counts are also recorded on
// their own keys, so the uncached count is input_tokens minus both.
func usageAttributes(u *usage) []attribute.KeyValue {
	if u == nil {
		return nil
	}
	var kv []attribute.KeyValue
	if u.InputTokens != nil {
		total := *u.InputTokens
		if u.CacheReadInputTokens != nil {
			total += *u.CacheReadInputTokens
		}
		if u.CacheCreationInputTokens != nil {
			total += *u.CacheCreationInputTokens
		}
		kv = append(kv, keyUsageInputTokens.Int64(total))
	}
	if u.OutputTokens != nil {
		kv = append(kv, keyUsageOutputTokens.Int64(*u.OutputTokens))
	}
	if u.CacheReadInputTokens != nil {
		kv = append(kv, keyUsageCacheReadInputTokens.Int64(*u.CacheReadInputTokens))
	}
	if u.CacheCreationInputTokens != nil {
		kv = append(kv, keyUsageCacheWriteInputTokens.Int64(*u.CacheCreationInputTokens))
	}
	return kv
}

// apiError is the body of an error response of the API.
type apiError struct {
	Error struct {
		Type string `json:"type"`
	} `json:"error"`
}

// recordAPIError records an error status. The span gets error.type (the API's
// error type, such as rate_limit_error, or the status code), never the error
// message: the message goes to output.value only, which FI_HIDE_OUTPUTS hides.
func (c *call) recordAPIError(res *http.Response) {
	data, _ := readResponseBody(res)
	typ := strconv.Itoa(res.StatusCode)
	var e apiError
	if json.Unmarshal(data, &e) == nil && e.Error.Type != "" {
		typ = e.Error.Type
	}
	if c.in.cfg.HideOutputs {
		c.set(keyOutputValue.String(redactedValue))
	} else if len(data) > 0 {
		v, _ := capValue(string(data))
		c.set(keyOutputValue.String(v), keyOutputMimeType.String(mimeJSON))
	}
	c.fail(typ)
}

// fail marks the span failed with an error type and, for a span that the
// middleware opened, ends it. The SDK sets the status of its own span.
func (c *call) fail(typ string) {
	if !c.own {
		return
	}
	c.set(keyErrorType.String(typ))
	c.span.SetStatus(codes.Error, typ)
	c.span.End()
}

// end ends a span that the middleware opened. A successful call keeps the
// status Unset, as OpenTelemetry instrumentations do.
func (c *call) end() {
	if c.own {
		c.span.End()
	}
}

// errorType returns a low-cardinality error.type for err. It never contains
// the error text, which can hold URLs, host names or paths.
func errorType(err error) string {
	switch {
	case errors.Is(err, context.Canceled):
		return errorTypeCancel
	case errors.Is(err, context.DeadlineExceeded):
		return "timeout"
	}
	var root error = err
	for {
		u := errors.Unwrap(root)
		if u == nil {
			break
		}
		root = u
	}
	return fmt.Sprintf("%T", root)
}
