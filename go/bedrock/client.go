// Package traceaibedrock adds bounded, privacy-aware GenAI spans to the AWS SDK
// for Go v2 Bedrock Runtime client. It uses your OpenTelemetry TracerProvider;
// it does not install providers, exporters, middleware, or credentials.
package traceaibedrock

import (
	"context"
	"errors"
	"time"

	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"
	"github.com/aws/smithy-go/transport/http/protocol/restjson1"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

const (
	Version   = "0.1.0" // x-release-please-version
	ScopeName = "github.com/future-agi/traceAI/go/bedrock"
)

// Runtime is the supported subset of *bedrockruntime.Client. All AWS options,
// signing, retries, deserialization and error values remain owned by the SDK.
type Runtime interface {
	Converse(context.Context, *bedrockruntime.ConverseInput, ...func(*bedrockruntime.Options)) (*bedrockruntime.ConverseOutput, error)
	ConverseStream(context.Context, *bedrockruntime.ConverseStreamInput, ...func(*bedrockruntime.Options)) (*bedrockruntime.ConverseStreamOutput, error)
	InvokeModel(context.Context, *bedrockruntime.InvokeModelInput, ...func(*bedrockruntime.Options)) (*bedrockruntime.InvokeModelOutput, error)
	InvokeModelWithResponseStream(context.Context, *bedrockruntime.InvokeModelWithResponseStreamInput, ...func(*bedrockruntime.Options)) (*bedrockruntime.InvokeModelWithResponseStreamOutput, error)
}

// Client observes four Bedrock Runtime operations without modifying the AWS client.
type Client struct {
	inner    Runtime
	provider trace.TracerProvider
	privacy  TraceConfig
}

// Option configures a new client adapter.
type Option func(*Client)

// WithTracerProvider uses p instead of the global OpenTelemetry provider.
func WithTracerProvider(p trace.TracerProvider) Option { return func(c *Client) { c.provider = p } }

// WithTraceConfig overrides the environment privacy settings.
func WithTraceConfig(p TraceConfig) Option { return func(c *Client) { c.privacy = p } }

// NewClient wraps an existing AWS client. Use the original client for unsupported
// operations (including bidirectional streams). Stream outputs expose the same
// Events, Err and Close methods via GetStream, but have distinct Go output types.
func NewClient(inner Runtime, opts ...Option) *Client {
	c := &Client{inner: inner, privacy: TraceConfigFromEnv()}
	for _, o := range opts {
		o(c)
	}
	return c
}

// The SDK's default restJson1 protocol holds a mutable event-stream codec on
// the client (smithy-go v1.28.7). Give each streaming operation its own protocol,
// including suppressed/unsampled/unknown-model calls. Keep the original client
// and all its other options; do not mutate caller slices or serialize requests.
// Run last so a per-call default-protocol override is isolated too. Custom
// protocols and Runtime implementations retain their own concurrency contract.
func streamOptions(opts []func(*bedrockruntime.Options)) []func(*bedrockruntime.Options) {
	out := make([]func(*bedrockruntime.Options), len(opts), len(opts)+1)
	copy(out, opts)
	return append(out, func(o *bedrockruntime.Options) {
		if _, ok := o.Protocol.(*restjson1.Protocol); ok {
			// restJson1 does not use the service schema argument.
			o.Protocol = restjson1.New(nil)
		}
	})
}

type contextKey uint8

const (
	suppressedKey contextKey = iota
	sessionKey
	userKey
)

// ContextWithSuppression skips this instrumentor without disabling native AWS spans.
func ContextWithSuppression(ctx context.Context) context.Context {
	return context.WithValue(ctx, suppressedKey, true)
}

// ContextWithSessionID carries an opaque session identifier on derived calls.
func ContextWithSessionID(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, sessionKey, id)
}

// ContextWithUserID carries an opaque end-user identifier on derived calls.
func ContextWithUserID(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, userKey, id)
}

func (c *Client) start(ctx context.Context, op string, model *string) (context.Context, *record) {
	if v, _ := ctx.Value(suppressedKey).(bool); v {
		return ctx, nil
	}
	p := c.provider
	if p == nil {
		p = otel.GetTracerProvider()
	}
	start := time.Now()
	attrs := []attribute.KeyValue{attribute.String("gen_ai.span.kind", "LLM"), attribute.String("gen_ai.operation.name", "chat"), attribute.String("gen_ai.provider.name", "aws.bedrock"), attribute.String("rpc.method", op)}
	if model != nil {
		attrs = append(attrs, attribute.String("gen_ai.request.model", bounded(*model, 1024)))
	}
	for _, v := range []struct {
		k    contextKey
		name string
	}{{sessionKey, "session.id"}, {userKey, "user.id"}} {
		if id, ok := ctx.Value(v.k).(string); ok && id != "" {
			attrs = append(attrs, attribute.String(v.name, bounded(id, 1024)))
		}
	}
	ctx, span := p.Tracer(ScopeName, trace.WithInstrumentationVersion(Version)).Start(ctx, "bedrock."+op, trace.WithSpanKind(trace.SpanKindClient), trace.WithTimestamp(start), trace.WithAttributes(attrs...))
	if !span.IsRecording() {
		span.End()
		return ctx, nil
	}
	return ctx, &record{span: span, privacy: c.privacy, started: start, blocks: make(map[int32]*block)}
}

func failure(span trace.Span, err error) {
	kind := "request_error"
	switch {
	case errors.Is(err, context.Canceled):
		kind = "canceled"
	case errors.Is(err, context.DeadlineExceeded):
		kind = "timeout"
	}
	// Never copy SDK errors: service/decoder errors can contain prompts, hostnames,
	// guardrail assessments, or raw event payloads. Return the original error intact.
	span.SetAttributes(attribute.String("error.type", kind))
	span.SetStatus(codes.Error, kind)
}

func (c *Client) Converse(ctx context.Context, in *bedrockruntime.ConverseInput, opts ...func(*bedrockruntime.Options)) (*bedrockruntime.ConverseOutput, error) {
	var model *string
	if in != nil {
		model = in.ModelId
	}
	ctx, r := c.start(ctx, "Converse", model)
	if r != nil && in != nil {
		r.converseInput(in.Messages, in.System, in.InferenceConfig, in.ToolConfig)
	}
	out, err := c.inner.Converse(ctx, in, opts...)
	if r != nil {
		if out != nil && err == nil {
			r.converseOutput(out)
		}
		r.finish(err, false)
	}
	return out, err
}

// ConverseStreamOutput preserves SDK result metadata without mutating its Reader.
type ConverseStreamOutput struct {
	*bedrockruntime.ConverseStreamOutput
	stream bedrockruntime.ConverseStreamOutputReader
}

func (o *ConverseStreamOutput) GetStream() bedrockruntime.ConverseStreamOutputReader { return o.stream }
func (c *Client) ConverseStream(ctx context.Context, in *bedrockruntime.ConverseStreamInput, opts ...func(*bedrockruntime.Options)) (*ConverseStreamOutput, error) {
	var model *string
	if in != nil {
		model = in.ModelId
	}
	ctx, r := c.start(ctx, "ConverseStream", model)
	if r != nil && in != nil {
		r.converseInput(in.Messages, in.System, in.InferenceConfig, in.ToolConfig)
	}
	out, err := c.inner.ConverseStream(ctx, in, streamOptions(opts)...)
	if out == nil {
		if r != nil {
			r.finish(err, false)
		}
		return nil, err
	}
	result := &ConverseStreamOutput{ConverseStreamOutput: out, stream: out.GetStream()}
	if r != nil {
		if err != nil || out.GetStream() == nil {
			r.finish(err, false)
		} else {
			result.stream = observeStream(ctx, out.GetStream(), r, r.converseEvent)
		}
	}
	return result, err
}

func (c *Client) InvokeModel(ctx context.Context, in *bedrockruntime.InvokeModelInput, opts ...func(*bedrockruntime.Options)) (*bedrockruntime.InvokeModelOutput, error) {
	var r *record
	if in != nil {
		if input := parseInvocation(in.ModelId, in.Body); input != nil {
			ctx, r = c.start(ctx, "InvokeModel", in.ModelId)
			if r != nil {
				r.invokeInput(input)
			}
		}
	}
	out, err := c.inner.InvokeModel(ctx, in, opts...)
	if r != nil {
		if out != nil && err == nil {
			r.invokeOutput(out.Body)
		}
		r.finish(err, false)
	}
	return out, err
}

// InvokeModelWithResponseStreamOutput preserves SDK metadata and exposes an observed reader.
type InvokeModelWithResponseStreamOutput struct {
	*bedrockruntime.InvokeModelWithResponseStreamOutput
	stream bedrockruntime.ResponseStreamReader
}

func (o *InvokeModelWithResponseStreamOutput) GetStream() bedrockruntime.ResponseStreamReader {
	return o.stream
}
func (c *Client) InvokeModelWithResponseStream(ctx context.Context, in *bedrockruntime.InvokeModelWithResponseStreamInput, opts ...func(*bedrockruntime.Options)) (*InvokeModelWithResponseStreamOutput, error) {
	var r *record
	if in != nil {
		if input := parseInvocation(in.ModelId, in.Body); input != nil {
			ctx, r = c.start(ctx, "InvokeModelWithResponseStream", in.ModelId)
			if r != nil {
				r.invokeInput(input)
			}
		}
	}
	out, err := c.inner.InvokeModelWithResponseStream(ctx, in, streamOptions(opts)...)
	if out == nil {
		if r != nil {
			r.finish(err, false)
		}
		return nil, err
	}
	result := &InvokeModelWithResponseStreamOutput{InvokeModelWithResponseStreamOutput: out, stream: out.GetStream()}
	if r != nil {
		if err != nil || out.GetStream() == nil {
			r.finish(err, false)
		} else {
			result.stream = observeStream(ctx, out.GetStream(), r, r.invokeEvent)
		}
	}
	return result, err
}
