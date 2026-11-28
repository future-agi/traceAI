package auto

import (
	"bytes"
	"errors"
	"log"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"testing"
)

const (
	ambientAPI    = "synthetic-api-value-0001"
	ambientSecret = "synthetic-secret-value-0002"
)

func ambientConfig() Config {
	return Config{BaseURL: "https://api.futureagi.com", APIKey: ambientAPI, SecretKey: ambientSecret}
}

func clearAmbient(t *testing.T) {
	t.Helper()
	clearProxyEnv(t)
	t.Setenv("OTEL_EXPORTER_OTLP_HEADERS", "")
	t.Setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "")
	t.Setenv("FI_API_KEY", "")
	t.Setenv("FI_SECRET_KEY", "")
}

// Ambient OTLP headers that carry a Future AGI key name or value are refused when the exporter is configured, value-free.
func TestKeyTransportAmbientHeadersCarryingKeysAreRefused(t *testing.T) {
	cases := []struct{ name, variable, value string }{
		{"name in the general variable", "OTEL_EXPORTER_OTLP_HEADERS", "X-Api-Key=other"},
		{"name in the traces variable", "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "X-Secret-Key=other"},
		{"name in any case", "OTEL_EXPORTER_OTLP_HEADERS", "x-aPi-KEY=other"},
		{"name beside another pair", "OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer%20abc, x-secret-key=other"},
		{"name with surrounding whitespace", "OTEL_EXPORTER_OTLP_HEADERS", "  X-Api-Key  =other"},
		{"percent-encoded name", "OTEL_EXPORTER_OTLP_HEADERS", "X%2DApi%2DKey=other"},
		{"secret value under an unrelated name", "OTEL_EXPORTER_OTLP_HEADERS", "x-trace-token=" + ambientSecret},
		{"api value inside a longer value", "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "x-trace-token=prefix-" + ambientAPI + "-suffix"},
		{"percent-encoded value", "OTEL_EXPORTER_OTLP_HEADERS", "x-trace-token=" + strings.ReplaceAll(ambientSecret, "-", "%2D")},
		{"encoded value in a pair beside a malformed escape", "OTEL_EXPORTER_OTLP_HEADERS", "x-a=50%,x-trace-token=" + strings.ReplaceAll(ambientSecret, "-", "%2D")},
		{"encoded value, malformed escape in the pair after it", "OTEL_EXPORTER_OTLP_HEADERS", "x-trace-token=" + strings.ReplaceAll(ambientAPI, "-", "%2D") + ",x-b=%zz"},
		{"encoded value with padding beside a malformed escape", "OTEL_EXPORTER_OTLP_HEADERS", "x-a=50%, x-trace-token =  " + strings.ReplaceAll(ambientSecret, "-", "%2D") + " "},
		{"encoded key name beside a malformed escape", "OTEL_EXPORTER_OTLP_HEADERS", "x-a=50%,X%2DApi%2DKey=v"},
		{"name and value split by whitespace", "OTEL_EXPORTER_OTLP_HEADERS", "x-ok=1 , x-trace-token = " + ambientAPI},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			clearAmbient(t)
			t.Setenv(c.variable, c.value)
			_, err := collectorEndpoint(ambientConfig())
			if !errors.Is(err, errKeyTransport) {
				t.Fatalf("want the key-transport refusal, got %v", err)
			}
			for _, secret := range []string{ambientAPI, ambientSecret, "other", c.value} {
				if strings.Contains(err.Error(), secret) {
					t.Fatalf("error not value-free: %v", err)
				}
			}
		})
	}
}

func TestKeyTransportAmbientKeyFromTheEnvironmentIsRefusedToo(t *testing.T) {
	clearAmbient(t)
	t.Setenv("FI_SECRET_KEY", "environment-secret-0003")
	t.Setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-trace-token=environment-secret-0003")
	if _, err := collectorEndpoint(ambientConfig()); !errors.Is(err, errKeyTransport) {
		t.Fatalf("want the key-transport refusal, got %v", err)
	}
}

func TestKeyTransportAnotherVendorsAmbientHeadersAreAllowed(t *testing.T) {
	clearAmbient(t)
	t.Setenv("VENDOR_API_TOKEN", "vendor-secret-value-0099")
	t.Setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-vendor-token=vendor-secret-value-0099,authorization=Bearer%20vendor-0099")
	t.Setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "x-other=short")
	if _, err := collectorEndpoint(ambientConfig()); err != nil {
		t.Fatalf("another vendor's headers refused: %v", err)
	}
}

// A zone literal keeps %25 in the written host and url.Hostname decodes it: the same host, never loopback.
func TestKeyTransportZoneLiterals(t *testing.T) {
	clearAmbient(t)
	if _, err := collectorEndpoint(Config{BaseURL: "https://[fe80::1%25en0]:4318"}); err != nil {
		t.Fatalf("https zoned link-local refused: %v", err)
	}
	if _, err := collectorEndpoint(Config{BaseURL: "http://[fe80::1%25eth0]:4318"}); !errors.Is(err, errKeyTransport) {
		t.Fatalf("plaintext zoned address without the opt-in must be refused, got %v", err)
	}
	t.Setenv("FI_ALLOW_INSECURE_HTTP", "1")
	buf := resetInsecureWarning(t)
	u, err := collectorEndpoint(Config{BaseURL: "http://[fe80::1%25eth0]:4318"})
	if err != nil {
		t.Fatalf("plaintext zoned address with the opt-in refused: %v", err)
	}
	if !strings.Contains(buf.String(), "FI_ALLOW_INSECURE_HTTP") {
		t.Fatalf("the opt-in warning was not logged: %q", buf.String())
	}
	if literalLoopback("fe80::1%eth0") || literalLoopback("::1%lo") {
		t.Fatal("a zoned address is never loopback")
	}
	if proxy, _ := exporterProxy(mustRequest(t, u)); proxy != nil {
		t.Fatalf("a plaintext send was proxied: %v", proxy)
	}
	// Anything else that is ambiguous stays refused.
	for _, base := range []string{"https://[fe80::1%41]:4318", "https://[fe80::1%25]x:4318", "https://[fe80::1%25]:4318"} {
		if _, err := collectorEndpoint(Config{BaseURL: base}); err == nil {
			t.Fatalf("%s must stay refused", base)
		}
	}
}

func mustRequest(t *testing.T, u *url.URL) *http.Request {
	t.Helper()
	req, err := http.NewRequest("POST", u.String(), nil)
	if err != nil {
		t.Fatal(err)
	}
	return req
}

// Package initialization runs the same check: a complete pair with wire capture on and ambient headers that name
// X-Api-Key arms nothing, reports the wire integration as failed, leaves http.DefaultTransport alone and warns once.
func TestKeyTransportArmRefusesAmbientKeyHeadersWithOneValueFreeWarning(t *testing.T) {
	ownedRegressionSetup(t)
	clearAmbient(t)
	t.Setenv("FI_BASE_URL", "https://api.futureagi.com")
	t.Setenv("FI_API_KEY", ambientAPI)
	t.Setenv("FI_SECRET_KEY", ambientSecret)
	t.Setenv("OTEL_EXPORTER_OTLP_HEADERS", "X-Api-Key=other-value-0004")
	var logs bytes.Buffer
	previous := log.Writer()
	log.SetOutput(&logs)
	baseTransport := http.DefaultTransport
	agent.Lock()
	agent.armed = nil
	warning = sync.Once{}
	agent.Unlock()
	t.Cleanup(func() { log.SetOutput(previous); http.DefaultTransport = baseTransport; warning = sync.Once{} })
	arm()
	arm()
	agent.Lock()
	armed, last := agent.armed, agent.last
	agent.Unlock()
	if armed != nil {
		t.Fatal("package initialization armed wire capture despite ambient key headers")
	}
	if http.DefaultTransport != baseTransport {
		t.Fatal("http.DefaultTransport was replaced")
	}
	status := ""
	for _, integration := range last.Integrations {
		if integration.Name == "wire" {
			status = integration.Status
		}
	}
	if status != "failed" {
		t.Fatalf("wire integration reported %q, want the failed status", status)
	}
	out := logs.String()
	if n := strings.Count(out, "instrumentation unavailable"); n != 1 {
		t.Fatalf("want exactly one warning, got %d: %q", n, out)
	}
	for _, secret := range []string{ambientAPI, ambientSecret, "other-value-0004"} {
		if strings.Contains(out, secret) {
			t.Fatalf("warning not value-free: %q", out)
		}
	}
}
