// Package auto installs bounded HTTP wire capture through a blank import, or
// Init. Go cannot patch compiled SDKs; dedicated middleware remains opt-in.
package auto

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"log"
	"math"
	"net/http"
	"net/url"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

type Option func(*settings)
type settings struct {
	config          []func(*Config)
	processor       sdktrace.SpanProcessor
	provider        *sdktrace.TracerProvider
	ignoreInherited map[string]bool
}

func configOption(update func(*Config), keys ...string) Option {
	return func(s *settings) {
		s.config = append(s.config, update)
		if s.ignoreInherited == nil {
			s.ignoreInherited = map[string]bool{}
		}
		for _, key := range keys {
			s.ignoreInherited[key] = true
		}
	}
}
func WithCredentials(apiKey, secretKey string) Option {
	keys := []string(nil)
	if apiKey != "" && secretKey != "" {
		keys = []string{"API_KEY", "SECRET_KEY"}
	}
	return configOption(func(c *Config) { c.APIKey = apiKey; c.SecretKey = secretKey }, keys...)
}
func WithConfig(config Config) Option {
	keys := []string{"*"}
	if config.APIKey == "" || config.SecretKey == "" {
		keys = append(keys, "credential_fallback")
	}
	return configOption(func(c *Config) { *c = config }, keys...)
}
func WithProject(name, kind string) Option {
	return configOption(func(c *Config) { c.ProjectName = name; c.ProjectType = kind }, "PROJECT_NAME", "PROJECT_TYPE")
}
func WithSampleRate(rate float64) Option {
	return configOption(func(c *Config) { c.SampleRate = rate }, "SAMPLE_RATE")
}
func WithWireCapture(enabled bool) Option {
	return configOption(func(c *Config) { c.WireCapture = enabled }, "WIRE_CAPTURE")
}
func WithTraceConfig(config TraceConfig) Option {
	return configOption(func(c *Config) { c.TraceConfig = config }, "HIDE_LLM_INVOCATION_PARAMETERS", "HIDE_INPUTS", "HIDE_OUTPUTS", "HIDE_INPUT_MESSAGES", "HIDE_OUTPUT_MESSAGES", "HIDE_INPUT_IMAGES", "HIDE_INPUT_TEXT", "HIDE_OUTPUT_TEXT", "BASE64_IMAGE_MAX_LENGTH")
}

// WithSpanProcessor replaces the network exporter, useful for local collectors
// and tests. With it, credentials are not needed.
func WithSpanProcessor(p sdktrace.SpanProcessor) Option { return func(s *settings) { s.processor = p } }
func WithTracerProvider(p *sdktrace.TracerProvider) Option {
	return func(s *settings) { s.provider = p }
}

var agent struct {
	sync.Mutex
	current  *agentState
	first    *agentState // Retained after Shutdown: initialization never rewires a process.
	last     AgentReport
	repeated sync.Once
}
var warning sync.Once

func warn() {
	warning.Do(func() {
		log.Print("traceai auto: instrumentation unavailable (check credentials, configuration and collector); application continues unchanged")
	})
}

// ShutdownHint is the one-time startup hint (S10). Go has no exit hook, so a
// program that returns from main right after its last AI call must flush.
const ShutdownHint = "traceai auto: active; spans are exported in batches and Go has no exit hook, so call auto.Shutdown(ctx) (or auto.ForceFlush) before main returns"

func init() {
	defer func() {
		if recover() != nil {
			warn()
		}
	}()
	if _, err := Init(context.Background()); err == nil {
		startupHint()
	}
}

// startupHint prints ShutdownHint once, from package initialization only, when
// the agent owns a batching FI exporter. Host-owned providers/processors flush
// with the application's own provider shutdown.
func startupHint() {
	agent.Lock()
	s := agent.current
	agent.Unlock()
	if s != nil && s.registration.queued {
		log.Print(ShutdownHint)
	}
}

type agentState struct {
	config         Config
	provider       *sdktrace.TracerProvider
	processor      sdktrace.SpanProcessor
	transport      http.RoundTripper
	res            *resource.Resource
	owned          bool
	once           sync.Once
	done           chan struct{}
	err            error
	active         atomic.Bool
	registration   *processorRegistration
	flushing       atomic.Bool
	nativeHooks    nativeCredentialHooks
	attributeLimit int
}

// Init configures the agent once per process. Later calls ignore options and
// return the original shutdown function, including after shutdown. A disabled
// blank import (for example without credentials) leaves explicit Init available.
// Initialization never contacts the collector.
// Errors are returned to explicit callers; the blank import logs one warning.
// Call Shutdown before short-lived programs exit; there is no Go atexit hook.
func Init(ctx context.Context, opts ...Option) (shutdown func(context.Context) error, err error) {
	shutdown = func(context.Context) error { return nil }
	defer func() {
		if recover() != nil {
			warn()
			err = fmt.Errorf("traceai auto initialization failed")
		}
	}()
	agent.Lock()
	defer agent.Unlock()
	if agent.first != nil {
		agent.repeated.Do(func() { log.Print("traceai auto: already initialized; options ignored") })
		return agent.first.shutdown, nil
	}
	s := settings{}
	for _, o := range opts {
		if o != nil {
			o(&s)
		}
	}
	c, err := readConfigWithOverrides(s.ignoreInherited, s.config...)
	if err != nil {
		warn()
		agent.last = makeReport(c, false, "failed")
		return shutdown, err
	}
	if c.Disabled {
		agent.last = makeReport(c, false, "disabled")
		return shutdown, nil
	}
	if c.ProjectType != "observe" && c.ProjectType != "experiment" || c.WireMaxBodyBytes <= 0 || math.IsNaN(c.SampleRate) || c.SampleRate < 0 || c.SampleRate > 1 {
		warn()
		agent.last = makeReport(c, false, "failed")
		return shutdown, fmt.Errorf("traceai auto invalid configuration")
	}
	provider := s.provider
	if provider == nil {
		provider, _ = otel.GetTracerProvider().(*sdktrace.TracerProvider)
	}
	hasCredentials := c.APIKey != "" && c.SecretKey != ""
	if s.processor == nil && provider == nil && !hasCredentials {
		warn()
		agent.last = makeReport(c, false, "disabled")
		return shutdown, nil
	}
	seedSecrets(c)
	// FI routing/metadata only. service.name is the OTel-resolved (or explicitly
	// configured) service, used for an agent-owned provider and to fill a host
	// resource that has none; it never replaces the application's identity (A6).
	kvs := []attribute.KeyValue{attribute.String("project_name", c.ProjectName), attribute.String("project_type", c.ProjectType), attribute.String("service.name", c.ServiceName), attribute.String("eval_tags", "[]"), attribute.String("metadata", "{}"), attribute.String("semantic_convention", "fi")}
	if c.ProjectVersionName != "" {
		kvs = append(kvs, attribute.String("project_version_name", c.ProjectVersionName))
	}
	if c.ProjectVersionID == "" {
		b := make([]byte, 16)
		_, _ = rand.Read(b)
		b[6] = (b[6] & 0x0f) | 0x40
		b[8] = (b[8] & 0x3f) | 0x80
		c.ProjectVersionID = fmt.Sprintf("%x-%x-%x-%x-%x", b[:4], b[4:6], b[6:8], b[8:10], b[10:])
	}
	kvs = append(kvs, attribute.String("project_version_id", c.ProjectVersionID))
	res := resource.NewSchemaless(kvs...)
	processor := s.processor
	// A host-owned provider is a complete destination on its own. Without an
	// explicit processor or FI pair, only install observation/dedup hooks; never
	// construct an unauthenticated FI exporter or take ownership of host processors.
	queued := processor == nil && hasCredentials
	if queued {
		u, parseErr := url.Parse(c.BaseURL)
		if parseErr != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil {
			warn()
			agent.last = makeReport(c, false, "failed")
			return shutdown, fmt.Errorf("invalid FI_BASE_URL")
		}
		u.Path = strings.TrimRight(u.Path, "/") + "/tracer/v1/traces"
		u.RawPath, u.Fragment, u.RawFragment = "", "", ""
		endpoint := u.String()
		// Export traffic must never traverse application instrumentation. Merely
		// removing our wire wrapper leaves arbitrary HTTP instrumentors in place.
		client := exporterClient(u.RawQuery, u.ForceQuery)
		exp, e := otlptracehttp.New(SuppressInstrumentation(ctx), otlptracehttp.WithEndpointURL(endpoint), otlptracehttp.WithHeaders(map[string]string{"X-Api-Key": c.APIKey, "X-Secret-Key": c.SecretKey}), otlptracehttp.WithCompression(otlptracehttp.NoCompression), otlptracehttp.WithHTTPClient(client), otlptracehttp.WithTimeout(5*time.Second))
		if e != nil {
			warn()
			agent.last = makeReport(c, false, "failed")
			return shutdown, fmt.Errorf("cannot configure traceai exporter")
		}
		processor = sdktrace.NewBatchSpanProcessor(&resourceExporter{SpanExporter: exp, client: client, config: c, res: res}, sdktrace.WithBatchTimeout(200*time.Millisecond), sdktrace.WithExportTimeout(5*time.Second))
	}
	state := &agentState{config: c, processor: processor, res: res, done: make(chan struct{})}
	state.registration = &processorRegistration{processor: processor, config: c, res: res, privacy: &spanPrivacy{}, queued: queued}
	if provider != nil {
		provider.RegisterSpanProcessor(state.registration)
		state.provider = provider
	} else {
		// Do not replace a third-party API provider whose processors cannot be
		// extended. A private provider can still parent wire spans to its context.
		owned, _ := resource.Merge(otelResource(), res)
		if owned == nil {
			owned = res
		}
		state.provider = sdktrace.NewTracerProvider(sdktrace.WithResource(owned), sdktrace.WithSampler(sdktrace.ParentBased(sdktrace.TraceIDRatioBased(c.SampleRate))), sdktrace.WithSpanProcessor(state.registration))
		state.owned = true
		if reflect.TypeOf(otel.GetTracerProvider()).String() == "*global.tracerProvider" {
			otel.SetTracerProvider(state.provider)
		}
	}
	state.attributeLimit = providerAttributeLimit(state.provider)
	if c.WireCapture && allowed(c, "wire") {
		state.transport = wrapTransport(http.DefaultTransport, state)
		http.DefaultTransport = state.transport
	}
	state.active.Store(true)
	state.registration.active.Store(true)
	agent.current, agent.first = state, state
	agent.last = makeReport(c, state.transport != nil, "")
	if c.Debug {
		b, _ := json.Marshal(agent.last)
		log.Printf("traceai auto: %s", b)
	}
	return state.shutdown, nil
}
func (s *agentState) shutdown(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	s.once.Do(func() {
		s.active.Store(false)
		s.registration.active.Store(false)
		agent.Lock()
		if agent.current == s {
			if s.transport != nil && http.DefaultTransport == s.transport {
				http.DefaultTransport = unwrapTransport(s.transport).(*wireTransport).base
			}
			agent.current = nil
			agent.last = makeReport(s.config, false, "disabled")
		}
		agent.Unlock()
		// Never invoke user callbacks under sync.Once or the agent lock. OTel's
		// unregister calls Shutdown(Background); our registration is a non-owning
		// adapter, so only the explicitly bounded agent shutdown reaches users.
		go func() {
			defer close(s.done)
			s.err = guarded(func() error {
				s.provider.UnregisterSpanProcessor(s.registration)
				s.registration.privacy.clear()
				if s.owned {
					_ = s.provider.Shutdown(ctx)
				}
				if s.processor != nil {
					return s.processor.Shutdown(ctx)
				}
				return nil
			})
		}()
	})
	select {
	case <-s.done:
		return s.err
	case <-ctx.Done():
		return ctx.Err()
	}
}
func Shutdown(ctx context.Context) error {
	agent.Lock()
	s := agent.current
	agent.Unlock()
	if s == nil {
		return nil
	}
	return s.shutdown(ctx)
}

// ForceFlush flushes queued agent spans without shutting down the provider.
func ForceFlush(ctx context.Context) error {
	agent.Lock()
	s := agent.current
	agent.Unlock()
	if s == nil {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if !s.flushing.CompareAndSwap(false, true) {
		return fmt.Errorf("traceai auto flush already in progress")
	}
	done := make(chan error, 1)
	go func() {
		defer s.flushing.Store(false)
		done <- guarded(func() error {
			if s.processor != nil {
				return s.processor.ForceFlush(ctx)
			}
			return s.provider.ForceFlush(ctx)
		})
	}()
	select {
	case err := <-done:
		return err
	case <-ctx.Done():
		return ctx.Err()
	}
}

type resourceExporter struct {
	sdktrace.SpanExporter
	client *http.Client
	config Config
	res    *resource.Resource
}

func (e *resourceExporter) ExportSpans(ctx context.Context, spans []sdktrace.ReadOnlySpan) (err error) {
	defer func() {
		if recover() != nil {
			warn()
			err = fmt.Errorf("traceai export failed")
		}
	}()
	// One authoritative boundary, immediately before OTLP serialization. Build
	// private views: a second application exporter keeps its original snapshots.
	private := make([]sdktrace.ReadOnlySpan, len(spans))
	for i, span := range spans {
		private[i] = exportSpan(span, e.config, e.res)
	}
	if err = e.SpanExporter.ExportSpans(SuppressInstrumentation(ctx), private); err != nil {
		warn()
		return nil
	}
	return nil
}
func (e *resourceExporter) Shutdown(ctx context.Context) error {
	if e.client != nil {
		defer e.client.CloseIdleConnections()
	}
	return e.SpanExporter.Shutdown(SuppressInstrumentation(ctx))
}
func (s *agentState) tracer() trace.Tracer {
	return s.provider.Tracer("github.com/future-agi/traceAI/go/auto")
}
