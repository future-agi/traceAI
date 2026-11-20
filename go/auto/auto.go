// Package auto installs bounded HTTP wire capture through one import, or
// Init. It never replaces the application's OpenTelemetry tracer provider. Go
// cannot patch compiled SDKs; dedicated middleware remains opt-in.
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
	module          bool // activation by a dedicated module's first span
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
	return configOption(func(c *Config) { *c = config; c.sampleRateSet = true }, keys...)
}
func WithProject(name, kind string) Option {
	return configOption(func(c *Config) { c.ProjectName = name; c.ProjectType = kind }, "PROJECT_NAME", "PROJECT_TYPE")
}
func WithSampleRate(rate float64) Option {
	return configOption(func(c *Config) { c.SampleRate = rate; c.sampleRateSet = true }, "SAMPLE_RATE")
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
	armed    *agentState // Package initialization's wire wrapper, until activation.
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

// Package initialization never changes the application's OpenTelemetry setup.
// It only arms wire capture; see arm.
func init() {
	defer func() {
		if recover() != nil {
			warn()
		}
	}()
	arm()
}

// arm is package initialization. When the environment or config file enables
// the agent with a complete FI pair and wire capture, it wraps
// http.DefaultTransport and prints the startup hint once. No tracer provider,
// processor or exporter exists until activation, which happens once: at the
// first explicit Init or the first captured AI request, whichever comes first.
// It runs the same network-free checks as Init, so an invalid configuration is
// reported (and warned about once) here.
func arm() {
	agent.Lock()
	defer agent.Unlock()
	c, err := readConfig()
	switch {
	case err != nil:
		warn()
		agent.last = makeReport(c, false, "failed")
		return
	case c.Disabled:
		agent.last = makeReport(c, false, "disabled")
		return
	case c.APIKey == "" || c.SecretKey == "":
		warn()
		agent.last = makeReport(c, false, "disabled")
		return
	case !validConfig(c):
		warn()
		agent.last = makeReport(c, false, "failed")
		return
	}
	if _, err := collectorEndpoint(c); err != nil {
		warn()
		agent.last = makeReport(c, false, "failed")
		return
	}
	if !c.WireCapture || !allowed(c, "wire") {
		// Nothing to arm: only an explicit Init activates the agent.
		agent.last = makeReport(c, false, "")
		return
	}
	state := &agentState{config: c, done: make(chan struct{}), hooksDone: make(chan struct{})}
	state.armed.Store(true)
	state.transport = wrapTransport(http.DefaultTransport, state)
	http.DefaultTransport = state.transport
	agent.armed = state
	agent.last = makeReport(c, true, "")
	log.Print(ShutdownHint)
}

// validConfig is the configuration check shared by arm and activate.
func validConfig(c Config) bool {
	return (c.ProjectType == "observe" || c.ProjectType == "experiment") && c.WireMaxBodyBytes > 0 && !math.IsNaN(c.SampleRate) && c.SampleRate >= 0 && c.SampleRate <= 1
}

// collectorEndpoint is the FI trace endpoint for a valid FI_BASE_URL.
func collectorEndpoint(c Config) (*url.URL, error) {
	u, err := url.Parse(c.BaseURL)
	if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil {
		return nil, fmt.Errorf("invalid FI_BASE_URL")
	}
	u.Path = strings.TrimRight(u.Path, "/") + "/tracer/v1/traces"
	u.RawPath, u.Fragment, u.RawFragment = "", "", ""
	return u, nil
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
	armed          atomic.Bool
	registration   *processorRegistration
	flushing       atomic.Bool
	nativeHooks    nativeCredentialHooks
	attributeLimit int
	ownedRatio     *ratioSampler // Only the known wrapper installed on our private provider.
	hooksStarted   atomic.Bool   // Shutdown hooks of dedicated modules run once (see runHooks).
	hooksDone      chan struct{}
}

// retire stops an armed wrapper that will not be activated. Inside an
// explicit Init or Shutdown the default transport is restored; after a
// failed activation on the request path the wrapper stays in place and passes
// every request through, because requests may be running.
func (s *agentState) retire(restore bool) {
	s.armed.Store(false)
	if restore && s.transport != nil && http.DefaultTransport == s.transport {
		http.DefaultTransport = unwrapTransport(s.transport).(*wireTransport).base
	}
}

// Init configures the agent once per process. Later calls, and calls after
// the first captured AI request has activated the agent, ignore options and
// return the original shutdown function, including after shutdown. Call Init
// before the first AI call for its options to apply. A disabled package
// initialization (for example without credentials) leaves explicit Init
// available. Initialization never contacts the collector and never replaces
// the OpenTelemetry global tracer provider.
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
	return activate(ctx, s, true)
}

// activate configures the agent, with the agent lock held. The tracer
// provider is WithTracerProvider's, else the application's global SDK
// provider if one is installed by now, else a private provider that only the
// agent's wire spans use. An armed wrapper is reused when wire capture stays
// on, so http.DefaultTransport is not replaced again; otherwise it is retired.
func activate(ctx context.Context, s settings, explicit bool) (shutdown func(context.Context) error, err error) {
	shutdown = func(context.Context) error { return nil }
	armed := agent.armed
	agent.armed = nil
	var state *agentState
	if armed != nil {
		// The wrapper stays armed while this runs, so a concurrent AI request
		// waits for the agent lock and is then captured, not passed through.
		defer func() {
			if state != armed || !armed.active.Load() {
				armed.retire(explicit)
			}
		}()
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
	if !validConfig(c) {
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
	limits := &exportLimits{} // Value-length limit, set once the provider is final.
	// A host-owned provider is a complete destination on its own. Without an
	// explicit processor or FI pair, only install observation/dedup hooks; never
	// construct an unauthenticated FI exporter or take ownership of host processors.
	queued := processor == nil && hasCredentials
	if queued {
		u, urlErr := collectorEndpoint(c)
		if urlErr != nil {
			warn()
			agent.last = makeReport(c, false, "failed")
			return shutdown, urlErr
		}
		// Export traffic must never traverse application instrumentation. Merely
		// removing our wire wrapper leaves arbitrary HTTP instrumentors in place.
		client := exporterClient(u.RawQuery, u.ForceQuery)
		exp, e := otlptracehttp.New(SuppressInstrumentation(ctx), otlptracehttp.WithEndpointURL(u.String()), otlptracehttp.WithHeaders(map[string]string{"X-Api-Key": c.APIKey, "X-Secret-Key": c.SecretKey}), otlptracehttp.WithCompression(otlptracehttp.NoCompression), otlptracehttp.WithHTTPClient(client), otlptracehttp.WithTimeout(5*time.Second))
		if e != nil {
			warn()
			agent.last = makeReport(c, false, "failed")
			return shutdown, fmt.Errorf("cannot configure traceai exporter")
		}
		processor = sdktrace.NewBatchSpanProcessor(&resourceExporter{SpanExporter: exp, client: client, config: c, res: res, limits: limits}, sdktrace.WithBatchTimeout(200*time.Millisecond), sdktrace.WithExportTimeout(5*time.Second))
	}
	capture := c.WireCapture && allowed(c, "wire")
	if s.module && armed == nil {
		// A module's first span can come while requests are in flight: never
		// replace http.DefaultTransport then. Only an agent armed at package
		// initialization captures wire spans.
		capture = false
	}
	if armed != nil && capture {
		// Fields are published by active.Store below; the armed wrapper reads
		// none of them before that.
		state = armed
	} else {
		state = &agentState{done: make(chan struct{}), hooksDone: make(chan struct{})}
	}
	state.config, state.processor, state.res = c, processor, res
	state.registration = &processorRegistration{processor: processor, config: c, res: res, privacy: &spanPrivacy{}, queued: queued, limits: limits}
	if provider != nil {
		provider.RegisterSpanProcessor(state.registration)
		state.provider = provider
		// Future AGI receives only AI spans from an application-owned provider;
		// its other spans stay with the application's own exporters.
		state.registration.aiOnly.Store(queued)
	} else {
		// Never install a global provider: the application's own setup, now or
		// later, stays in charge of its spans. This private provider carries only
		// the agent's wire spans, which still take their parent from the request
		// context, so they join the application's trace.
		owned, _ := resource.Merge(otelResource(), res)
		if owned == nil {
			owned = res
		}
		sampler := configuredSampler(c)
		if ratio, ok := sampler.(ratioSampler); ok {
			state.ownedRatio = &ratio
		}
		state.provider = sdktrace.NewTracerProvider(sdktrace.WithResource(owned), sdktrace.WithSampler(sampler), sdktrace.WithSpanProcessor(state.registration))
		state.owned = true
	}
	state.attributeLimit = providerAttributeLimit(state.provider)
	// Before activation: the export boundary detects value truncation with it.
	limits.valueLength.Store(int64(providerValueLengthLimit(state.provider)))
	if capture && state.transport == nil {
		state.transport = wrapTransport(http.DefaultTransport, state)
		http.DefaultTransport = state.transport
	}
	state.registration.active.Store(true)
	state.active.Store(true)
	state.armed.Store(false)
	agent.current, agent.first = state, state
	moduleState.Store(state)
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
	s.runHooks(ctx)
	s.once.Do(func() {
		s.active.Store(false)
		s.registration.active.Store(false)
		agent.Lock()
		if agent.current == s {
			moduleState.CompareAndSwap(s, nil)
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
	if s == nil && agent.armed != nil {
		// Before the first AI call there is nothing to flush; stop arming.
		agent.armed.retire(true)
		agent.armed = nil
	}
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
	limits *exportLimits
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
		private[i] = exportSpanLimited(span, e.config, e.res, e.limits.value())
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
	return s.provider.Tracer(wireScope)
}
