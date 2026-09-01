package traceai

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestDefaultConfigReadsEnv(t *testing.T) {
	t.Setenv(envProjectName, "my-project")
	t.Setenv(envBaseURL, "https://example.test")
	t.Setenv(envAPIKey, "key")
	t.Setenv(envSecretKey, "secret")

	cfg := DefaultConfig()

	if cfg.ProjectName != "my-project" {
		t.Errorf("ProjectName = %q, want my-project", cfg.ProjectName)
	}
	if cfg.BaseURL != "https://example.test" {
		t.Errorf("BaseURL = %q, want https://example.test", cfg.BaseURL)
	}
	if cfg.APIKey != "key" || cfg.SecretKey != "secret" {
		t.Errorf("credentials = %q/%q, want key/secret", cfg.APIKey, cfg.SecretKey)
	}
	if cfg.GRPCURL != defaultGRPCURL {
		t.Errorf("GRPCURL = %q, want the default %q", cfg.GRPCURL, defaultGRPCURL)
	}
}

func TestRegisterHTTPWiring(t *testing.T) {
	type received struct {
		path      string
		apiKey    string
		secretKey string
	}
	got := make(chan received, 1)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case got <- received{r.URL.Path, r.Header.Get(headerAPIKey), r.Header.Get(headerSecretKey)}:
		default:
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	provider, err := Register(
		WithBaseURL(srv.URL),
		WithCredentials("key", "secret"),
		WithBatchExport(false),
		WithSetGlobal(false),
	)
	if err != nil {
		t.Fatalf("Register: %v", err)
	}

	_, span := provider.TracerProvider().Tracer("test").Start(context.Background(), "span")
	span.End()

	if err := provider.Shutdown(context.Background()); err != nil {
		t.Fatalf("Shutdown: %v", err)
	}

	select {
	case r := <-got:
		if r.path != "/tracer/v1/traces" {
			t.Errorf("path = %q, want /tracer/v1/traces", r.path)
		}
		if r.apiKey != "key" {
			t.Errorf("%s = %q, want key", headerAPIKey, r.apiKey)
		}
		if r.secretKey != "secret" {
			t.Errorf("%s = %q, want secret", headerSecretKey, r.secretKey)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("exporter never reached the server")
	}
}

// default GRPCURL has a scheme, WithEndpoint would choke on it
func TestNewExporterGRPCAcceptsSchemeURL(t *testing.T) {
	cfg := DefaultConfig()
	cfg.Transport = TransportGRPC

	exporter, err := newExporter(context.Background(), cfg)
	if err != nil {
		t.Fatalf("newExporter(grpc) with %q: %v", cfg.GRPCURL, err)
	}
	if err := exporter.Shutdown(context.Background()); err != nil {
		t.Errorf("Shutdown: %v", err)
	}
}
