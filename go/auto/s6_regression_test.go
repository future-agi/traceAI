package auto

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/future-agi/traceAI/go/auto/internal/wire"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

func s6Attribute(value any) attribute.Value {
	switch x := value.(type) {
	case nil:
		return attribute.Value{}
	case string:
		return attribute.StringValue(x)
	case bool:
		return attribute.BoolValue(x)
	case float64:
		return attribute.Float64Value(x)
	case []any:
		out := make([]attribute.Value, len(x))
		for i, item := range x {
			out[i] = s6Attribute(item)
		}
		return attribute.SliceValue(out...)
	case map[string]any:
		keys := make([]string, 0, len(x))
		for key := range x {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		out := make([]attribute.KeyValue, 0, len(x))
		for _, key := range keys {
			out = append(out, attribute.KeyValue{Key: attribute.Key(key), Value: s6Attribute(x[key])})
		}
		return attribute.MapValue(out...)
	default:
		panic("unexpected fixture type")
	}
}

// Exercise the native OTel adapter, not only the JSON/reference-facing helper.
// attrs is the identical export path for spans, resources, events, links, scopes.
func TestS6NativeExportPrivacyFixtures(t *testing.T) {
	data, err := os.ReadFile("internal/wire/privacy-fixtures.json")
	if err != nil {
		t.Fatal(err)
	}
	var bundle struct {
		Cases []struct {
			Name  string
			Input struct {
				Site, Path string
				Value      any
				Siblings   map[string]any
				Secrets    []string `json:"known_secrets"`
			}
			Expected any
		}
	}
	if err = json.Unmarshal(data, &bundle); err != nil {
		t.Fatal(err)
	}
	for _, fixture := range bundle.Cases {
		// Free-text fixtures deliberately do not supply a whole-value boundary.
		// Their scanner is covered by TestPrivacyFixtures in internal/wire.
		if fixture.Input.Site == "free_text_urls" {
			continue
		}
		t.Run(fixture.Name, func(t *testing.T) {
			var registry secretRegistry
			for _, secret := range fixture.Input.Secrets {
				registry.register(secret)
			}
			s := &spanSanitizer{secrets: registry.snapshot(), nodes: 16384, bytes: 32 * 1024 * 1024}
			attrs := []attribute.KeyValue{{Key: attribute.Key(fixture.Input.Path), Value: s6Attribute(fixture.Input.Value)}}
			for name, value := range fixture.Input.Siblings {
				attrs = append(attrs, attribute.KeyValue{Key: attribute.Key(name), Value: s6Attribute(value)})
			}
			before := fmt.Sprint(attrs)
			clean := s.attrs(attrs, false, 0, 0)
			if fmt.Sprint(attrs) != before {
				t.Fatal("mutated host-owned attributes")
			}
			var got any
			for _, kv := range clean {
				if string(kv.Key) == fixture.Input.Path {
					got = s.native(kv.Value, 0)
					break
				}
			}
			if !reflect.DeepEqual(got, fixture.Expected) {
				t.Fatalf("got %#v; want %#v", got, fixture.Expected)
			}
		})
	}
}

func TestS6AtomicCredentialPairs(t *testing.T) {
	for _, tc := range []struct {
		name, envAPI, envSecret, fileAPI, fileSecret, codeAPI, codeSecret, wantAPI, wantSecret, missing string
		withConfig                                                                                      bool
	}{
		{name: "env-api-only", envAPI: "ENV_API_PRIVATE", fileAPI: "FILE_API_PRIVATE", fileSecret: "FILE_SECRET_PRIVATE", wantAPI: "FILE_API_PRIVATE", wantSecret: "FILE_SECRET_PRIVATE", missing: "FI_SECRET_KEY"},
		{name: "env-secret-only", envSecret: "ENV_SECRET_PRIVATE", fileAPI: "FILE_API_PRIVATE", fileSecret: "FILE_SECRET_PRIVATE", wantAPI: "FILE_API_PRIVATE", wantSecret: "FILE_SECRET_PRIVATE", missing: "FI_API_KEY"},
		{name: "file-api-only", fileAPI: "FILE_API_PRIVATE", missing: "FI_SECRET_KEY"},
		{name: "file-secret-only", fileSecret: "FILE_SECRET_PRIVATE", missing: "FI_API_KEY"},
		{name: "opposite-halves-never-combine", envAPI: "ENV_API_PRIVATE", fileSecret: "FILE_SECRET_PRIVATE", missing: "FI_SECRET_KEY"},
		{name: "env-complete", envAPI: "ENV_API_PRIVATE", envSecret: "ENV_SECRET_PRIVATE", fileAPI: "FILE_API_PRIVATE", fileSecret: "FILE_SECRET_PRIVATE", wantAPI: "ENV_API_PRIVATE", wantSecret: "ENV_SECRET_PRIVATE"},
		{name: "code-complete", envAPI: "ENV_API_PRIVATE", envSecret: "ENV_SECRET_PRIVATE", codeAPI: "CODE_API_PRIVATE", codeSecret: "CODE_SECRET_PRIVATE", wantAPI: "CODE_API_PRIVATE", wantSecret: "CODE_SECRET_PRIVATE"},
		{name: "code-api-only", envAPI: "ENV_API_PRIVATE", envSecret: "ENV_SECRET_PRIVATE", codeAPI: "CODE_API_PRIVATE", wantAPI: "ENV_API_PRIVATE", wantSecret: "ENV_SECRET_PRIVATE", missing: "FI_SECRET_KEY"},
		{name: "code-secret-only", fileAPI: "FILE_API_PRIVATE", fileSecret: "FILE_SECRET_PRIVATE", codeSecret: "CODE_SECRET_PRIVATE", wantAPI: "FILE_API_PRIVATE", wantSecret: "FILE_SECRET_PRIVATE", missing: "FI_API_KEY"},
		{name: "config-api-only", fileAPI: "FILE_API_PRIVATE", fileSecret: "FILE_SECRET_PRIVATE", codeAPI: "CODE_API_PRIVATE", wantAPI: "FILE_API_PRIVATE", wantSecret: "FILE_SECRET_PRIVATE", missing: "FI_SECRET_KEY", withConfig: true},
		{name: "config-secret-only", envAPI: "ENV_API_PRIVATE", envSecret: "ENV_SECRET_PRIVATE", codeSecret: "CODE_SECRET_PRIVATE", wantAPI: "ENV_API_PRIVATE", wantSecret: "ENV_SECRET_PRIVATE", missing: "FI_API_KEY", withConfig: true},
		{name: "empty-env-falls-back", fileAPI: "FILE_API_PRIVATE", fileSecret: "FILE_SECRET_PRIVATE", wantAPI: "FILE_API_PRIVATE", wantSecret: "FILE_SECRET_PRIVATE"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("FI_API_KEY", tc.envAPI)
			t.Setenv("FI_SECRET_KEY", tc.envSecret)
			file := filepath.Join(t.TempDir(), "futureagi.toml")
			if err := os.WriteFile(file, []byte(fmt.Sprintf("api_key = %q\nsecret_key = %q\n", tc.fileAPI, tc.fileSecret)), 0600); err != nil {
				t.Fatal(err)
			}
			t.Setenv("FI_CONFIG_FILE", file)
			var logs bytes.Buffer
			old := log.Writer()
			log.SetOutput(&logs)
			defer log.SetOutput(old)
			s := settings{}
			if tc.withConfig {
				WithConfig(Config{APIKey: tc.codeAPI, SecretKey: tc.codeSecret, SampleRate: 1, WireMaxBodyBytes: 4194304})(&s)
			} else if tc.codeAPI != "" || tc.codeSecret != "" {
				WithCredentials(tc.codeAPI, tc.codeSecret)(&s)
			}
			c, err := readConfigWithOverrides(s.ignoreInherited, s.config...)
			if err != nil {
				t.Fatal(err)
			}
			if c.APIKey != tc.wantAPI || c.SecretKey != tc.wantSecret {
				t.Fatal("credential pair crossed source boundaries")
			}
			if tc.missing != "" && !strings.Contains(logs.String(), "missing "+tc.missing) {
				t.Fatalf("missing safe warning: %q", logs.String())
			}
			if strings.Contains(logs.String(), "PRIVATE") {
				t.Fatal("credential value appeared in warning")
			}
		})
	}
}

func TestS6TransportCatalogObservation(t *testing.T) {
	var catalog struct {
		Credentials struct {
			Patterns   []struct{ Example string }
			Exclusions []struct{ Examples []string }
		}
	}
	if err := json.Unmarshal(wire.PrivacyCatalogJSON, &catalog); err != nil {
		t.Fatal(err)
	}
	for _, pattern := range catalog.Credentials.Patterns {
		for _, site := range []string{"header", "query", "body"} {
			t.Run(site+"/"+pattern.Example, func(t *testing.T) {
				ownedRegressionSetup(t)
				rec := tracetest.NewSpanRecorder()
				if _, err := InitForTest(context.Background(), WithSpanProcessor(rec)); err != nil {
					t.Fatal(err)
				}
				secret := "S6_TRANSPORT_" + site + "_PRIVATE"
				target := "http://127.0.0.1:12345/v1/chat/completions"
				body := map[string]any{"model": "m", "messages": []any{map[string]any{"role": "user", "content": "hi"}}}
				if site == "query" {
					target += "?" + url.QueryEscape(pattern.Example) + "=" + url.QueryEscape(secret)
				}
				if site == "body" {
					body[pattern.Example] = map[string]any{"nested": []any{secret}}
				}
				data, _ := json.Marshal(body)
				req, _ := http.NewRequest("POST", target, bytes.NewReader(data))
				req.Header.Set("Content-Type", "application/json")
				if site == "header" {
					req.Header.Set(pattern.Example, secret)
				}
				// Suppressed calls observe only headers/query, never buffer bodies.
				if site != "body" {
					req = req.WithContext(SuppressInstrumentation(req.Context()))
				}
				base := round4TransportFunc(func(r *http.Request) (*http.Response, error) {
					_, _ = io.Copy(io.Discard, r.Body)
					_ = r.Body.Close()
					return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}}, Body: io.NopCloser(strings.NewReader(`{"choices":[{"message":{"role":"assistant","content":"ok"}}]}`)), Request: r}, nil
				})
				response, err := Transport(base).RoundTrip(req)
				if err != nil {
					t.Fatal(err)
				}
				_, _ = io.Copy(io.Discard, response.Body)
				_ = response.Body.Close()
				if processSecrets.snapshot().replace(secret) != "<redacted>" {
					t.Fatal("catalog credential crossed transport without being registered")
				}
			})
		}
	}
	for _, exclusion := range catalog.Credentials.Exclusions {
		for _, name := range exclusion.Examples {
			if values := wire.CredentialValues(http.Header{name: {"S6_ORDINARY_PRIVATE"}}, "https://example.invalid/?"+url.QueryEscape(name)+"=S6_ORDINARY_PRIVATE"); len(values) != 0 {
				t.Errorf("ordinary metric/identifier harvested: %s", name)
			}
		}
	}
}

func TestS6IndexedNonAudioMediaAllSurfaces(t *testing.T) {
	for _, key := range []string{"output.b64_json.0", "input.file_data.0", "input.images.0", "gen_ai.input.messages.0.parts.0.inlineData.data.0"} {
		t.Run(key, func(t *testing.T) {
			png := base64.StdEncoding.EncodeToString(append([]byte{0x89, 'P', 'N', 'G', 13, 10, 26, 10}, bytes.Repeat([]byte{1}, 3170)...))
			attrs := []attribute.KeyValue{attribute.String(key, png), attribute.String("gen_ai.input.messages.0.parts.0.inlineData.mimeType", "image/png"), attribute.String("neighbor.content", "safe neighbor")}
			recorder := tracetest.NewSpanRecorder()
			p := sdktrace.NewTracerProvider(sdktrace.WithResource(resource.NewSchemaless(attrs...)), sdktrace.WithSpanProcessor(recorder))
			defer p.Shutdown(context.Background())
			parent := trace.NewSpanContext(trace.SpanContextConfig{TraceID: trace.TraceID{1}, SpanID: trace.SpanID{1}})
			_, span := p.Tracer("s6", trace.WithInstrumentationAttributes(attrs...)).Start(context.Background(), "s6", trace.WithAttributes(attrs...), trace.WithLinks(trace.Link{SpanContext: parent, Attributes: attrs}))
			span.AddEvent("s6", trace.WithAttributes(attrs...))
			span.End()
			view := exportSpan(recorder.Ended()[0], Config{}, resource.Empty())
			scope := view.InstrumentationScope()
			for _, surface := range [][]attribute.KeyValue{view.Attributes(), view.Resource().Attributes(), view.Events()[0].Attributes, view.Links()[0].Attributes, scope.Attributes.ToSlice()} {
				encoded := attribute.Map("view", surface...).String()
				if strings.Contains(encoded, png) || !strings.Contains(encoded, "safe neighbor") {
					t.Fatal("indexed media leaked or safe neighbors disappeared")
				}
			}
			if spanAttr(recorder.Ended()[0], key).AsString() != png {
				t.Fatal("host snapshot was mutated")
			}
		})
	}
}
