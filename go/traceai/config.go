// Package traceai sets up OTel tracing with Future AGI defaults.
package traceai

import (
	"context"
	"fmt"
	"os"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
	"go.opentelemetry.io/otel/trace"
)

const (
	envAPIKey           = "FI_API_KEY"
	envSecretKey        = "FI_SECRET_KEY"
	envBaseURL          = "FI_BASE_URL"
	envGRPCURL          = "FI_GRPC_URL"
	envProjectName      = "FI_PROJECT_NAME"

	defaultBaseURL      = "https://api.futureagi.com"
	defaultGRPCURL      = "https://grpc.futureagi.com"
	defaultProjectName  = "DEFAULT_PROJECT_NAME"

	headerAPIKey    = "X-Api-Key"
	headerSecretKey = "X-Secret-Key"
)

// Transport specifies the OTLP export protocol.
type Transport int

const (
	TransportHTTP Transport = iota
	TransportGRPC
)

// Config holds the options for initializing a traceAI TracerProvider.
type Config struct {
	ProjectName  string
	Transport    Transport
	BaseURL      string
	GRPCURL      string
	APIKey       string
	SecretKey    string
	BatchExport  bool
	SetGlobal    bool
	ShutdownTimeout time.Duration
}

// DefaultConfig returns a Config populated from environment variables.
func DefaultConfig() Config {
	return Config{
		ProjectName:     envOrDefault(envProjectName, defaultProjectName),
		Transport:       TransportHTTP,
		BaseURL:         envOrDefault(envBaseURL, defaultBaseURL),
		GRPCURL:         envOrDefault(envGRPCURL, defaultGRPCURL),
		APIKey:          os.Getenv(envAPIKey),
		SecretKey:       os.Getenv(envSecretKey),
		BatchExport:     true,
		SetGlobal:       true,
		ShutdownTimeout: 10 * time.Second,
	}
}

type Option func(*Config)

func WithProjectName(name string) Option {
	return func(c *Config) { c.ProjectName = name }
}

func WithTransport(t Transport) Option {
	return func(c *Config) { c.Transport = t }
}

func WithBaseURL(url string) Option {
	return func(c *Config) { c.BaseURL = url }
}

func WithCredentials(apiKey, secretKey string) Option {
	return func(c *Config) {
		c.APIKey = apiKey
		c.SecretKey = secretKey
	}
}

func WithBatchExport(batch bool) Option {
	return func(c *Config) { c.BatchExport = batch }
}

func WithSetGlobal(global bool) Option {
	return func(c *Config) { c.SetGlobal = global }
}

func WithShutdownTimeout(d time.Duration) Option {
	return func(c *Config) { c.ShutdownTimeout = d }
}

// Provider wraps a TracerProvider with shutdown handling.
type Provider struct {
	tp              *sdktrace.TracerProvider
	shutdownTimeout time.Duration
}

// Register creates and configures a new traceAI TracerProvider.
// It returns a Provider whose Shutdown method should be called on process exit.
//
//	provider, err := traceai.Register(
//	    traceai.WithProjectName("my-llm-service"),
//	)
//	if err != nil {
//	    log.Fatal(err)
//	}
//	defer provider.Shutdown(context.Background())
func Register(opts ...Option) (*Provider, error) {
	cfg := DefaultConfig()
	for _, o := range opts {
		o(&cfg)
	}

	ctx := context.Background()

	exporter, err := newExporter(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("traceai: create exporter: %w", err)
	}

	res, err := resource.Merge(
		resource.Default(),
		resource.NewWithAttributes(
			semconv.SchemaURL,
			semconv.ServiceName(cfg.ProjectName),
		),
	)
	if err != nil {
		return nil, fmt.Errorf("traceai: create resource: %w", err)
	}

	var spanProcessor sdktrace.SpanProcessor
	if cfg.BatchExport {
		spanProcessor = sdktrace.NewBatchSpanProcessor(exporter)
	} else {
		spanProcessor = sdktrace.NewSimpleSpanProcessor(exporter)
	}

	tp := sdktrace.NewTracerProvider(
		sdktrace.WithResource(res),
		sdktrace.WithSpanProcessor(spanProcessor),
	)

	if cfg.SetGlobal {
		otel.SetTracerProvider(tp)
	}

	return &Provider{
		tp:              tp,
		shutdownTimeout: cfg.ShutdownTimeout,
	}, nil
}

func (p *Provider) TracerProvider() trace.TracerProvider {
	return p.tp
}

// Shutdown flushes pending spans and shuts down the exporter.
func (p *Provider) Shutdown(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, p.shutdownTimeout)
	defer cancel()
	return p.tp.Shutdown(ctx)
}

func newExporter(ctx context.Context, cfg Config) (sdktrace.SpanExporter, error) {
	headers := make(map[string]string)
	if cfg.APIKey != "" {
		headers[headerAPIKey] = cfg.APIKey
	}
	if cfg.SecretKey != "" {
		headers[headerSecretKey] = cfg.SecretKey
	}

	switch cfg.Transport {
	case TransportGRPC:
		return otlptracegrpc.New(ctx,
			otlptracegrpc.WithEndpoint(cfg.GRPCURL),
			otlptracegrpc.WithHeaders(headers),
		)
	default:
		endpoint := cfg.BaseURL + "/tracer/v1/traces"
		return otlptracehttp.New(ctx,
			otlptracehttp.WithEndpointURL(endpoint),
			otlptracehttp.WithHeaders(headers),
		)
	}
}

func envOrDefault(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
