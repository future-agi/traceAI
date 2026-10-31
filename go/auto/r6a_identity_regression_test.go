package auto

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

// A6 (2026-10-31): the application's resource identity is authoritative in the
// FI export view; FI routing is the separate project_name attribute; the agent
// never uses the FI project (or its working-directory default) as service.name.
func TestR6aResourceIdentityOTLP(t *testing.T) {
	exe, _ := os.Executable()
	fallback := "unknown_service:" + filepath.Base(exe)
	for _, tc := range []struct {
		name, otelService, resourceEnv string
		host                           *resource.Resource
		want                           map[string]string
	}{
		{"host-wins-over-env", "env-checkout", "", resource.NewSchemaless(attribute.String("service.name", "host-checkout"), attribute.String("service.namespace", "shop"), attribute.String("deployment.environment", "prod")), map[string]string{"service.name": "host-checkout", "service.namespace": "shop", "deployment.environment": "prod"}},
		{"host-without-service", "", "", resource.NewSchemaless(attribute.String("service.namespace", "shop")), map[string]string{"service.name": fallback, "service.namespace": "shop"}},
		{"agent-owned-otel-fallback", "", "deployment.environment=staging,service.namespace=ns", nil, map[string]string{"service.name": fallback, "deployment.environment": "staging", "service.namespace": "ns"}},
		{"agent-owned-env-service", "env-svc", "", nil, map[string]string{"service.name": "env-svc"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ownedRegressionSetup(t)
			received := privacyCollector(t)
			t.Setenv("OTEL_SERVICE_NAME", tc.otelService)
			t.Setenv("OTEL_RESOURCE_ATTRIBUTES", tc.resourceEnv)
			t.Setenv("FI_PROJECT_NAME", "billing-project")
			opts := []Option{WithCredentials("R6A_IDENTITY_FI_KEY", "R6A_IDENTITY_FI_SECRET")}
			var host *tracetest.SpanRecorder
			var p *sdktrace.TracerProvider
			if tc.host != nil {
				host = tracetest.NewSpanRecorder()
				p = sdktrace.NewTracerProvider(sdktrace.WithResource(tc.host), sdktrace.WithSpanProcessor(host))
				defer p.Shutdown(context.Background())
				opts = append(opts, WithTracerProvider(p))
			}
			if _, err := InitForTest(context.Background(), opts...); err != nil {
				t.Fatal(err)
			}
			if p == nil {
				p = agent.current.provider
			}
			_, span := p.Tracer("application").Start(context.Background(), "application")
			span.End()
			request := flushedPrivacyExport(t, received)
			got := map[string]string{}
			for _, kv := range request.ResourceSpans[0].Resource.Attributes {
				got[kv.Key] = kv.Value.GetStringValue()
			}
			for key, want := range tc.want {
				if got[key] != want {
					t.Errorf("%s=%q want %q (resource %v)", key, got[key], want, got)
				}
			}
			if got["project_name"] != "billing-project" || got["service.name"] == "billing-project" {
				t.Fatalf("FI routing must be separate from service identity: %v", got)
			}
			if wd, _ := os.Getwd(); tc.otelService == "" && got["service.name"] == filepath.Base(wd) {
				t.Fatal("working-directory project default used as service.name")
			}
			if host != nil {
				original := host.Ended()[0].Resource().Set()
				if _, routed := original.Value("project_name"); routed {
					t.Fatal("FI routing leaked into the application's own resource")
				}
				for _, kv := range tc.host.Attributes() {
					if v, _ := original.Value(kv.Key); v.Emit() != kv.Value.Emit() {
						t.Fatalf("application resource mutated: %s", kv.Key)
					}
				}
			}
		})
	}
}
