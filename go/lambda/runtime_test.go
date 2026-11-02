package traceailambda

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/aws/aws-lambda-go/lambda"
	"github.com/aws/aws-lambda-go/lambdaurl"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// These tests run handlers through aws-lambda-go's real runtime loop against a
// local fake Runtime API, because the response Content-Type is chosen by that
// loop and is invisible to direct Invoke calls.

const runtimeCaseEnv = "TRACEAI_LAMBDA_RUNTIME_CASE"

const functionURLEvent = `{"version":"2.0","rawPath":"/","headers":{"traceparent":"00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"},"requestContext":{"domainName":"example.lambda-url.us-east-1.on.aws","http":{"method":"GET","path":"/","sourceIp":"127.0.0.1"}},"body":""}`

type contextOptionKey struct{}

func contextOptionValue(ctx context.Context) string {
	value, _ := ctx.Value(contextOptionKey{}).(string)
	return value
}

func detectedPage(w http.ResponseWriter, _ *http.Request) {
	_, _ = io.WriteString(w, "<!DOCTYPE html><html></html>")
}

// runtimeCases start a handler without and with WrapHandler. A plain start
// that takes options uses lambda.StartWithOptions with the handler function,
// because aws-lambda-go ignores options for handlers built by lambda.NewHandler.
var runtimeCases = map[string]struct{ plain, wrapped func(Provider) }{
	"json": sdkCase(func(context.Context) (map[string]string, error) {
		return map[string]string{"ok": "yes"}, nil
	}),
	"no-result": sdkCase(func(context.Context) error { return nil }),
	"reader": sdkCase(func(context.Context) (io.Reader, error) {
		return strings.NewReader("raw bytes"), nil
	}),
	"function-url-stream": sdkCase(lambdaurl.Wrap(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		_, _ = io.WriteString(w, "hello")
	}))),
	"function-url-detect-option": sdkCase(lambdaurl.Wrap(http.HandlerFunc(detectedPage)), lambdaurl.WithDetectContentType(true)),
	"context-option":             sdkCase(contextOptionHandler, lambda.WithContextValue(contextOptionKey{}, "from-option")),
	"error": sdkCase(func(context.Context) (string, error) {
		return "", errSyntheticFailure
	}),
	"nil-provider-context-option": {
		plain: func(Provider) {
			lambda.StartWithOptions(contextOptionHandler, lambda.WithContextValue(contextOptionKey{}, "from-option"))
		},
		wrapped: func(Provider) {
			lambda.StartHandler(WrapHandler(lambda.NewHandler(contextOptionHandler), nil, lambda.WithContextValue(contextOptionKey{}, "from-option")))
		},
	},
	"custom-bytes": {
		plain: func(Provider) { lambda.StartHandler(customBytes) },
		wrapped: func(provider Provider) {
			lambda.StartHandler(WrapHandler(customBytes, provider))
		},
	},
	"custom-bytes-context-option": {
		plain: func(Provider) {
			lambda.StartWithOptions(customBytes, lambda.WithContextValue(contextOptionKey{}, "from-option"))
		},
		wrapped: func(provider Provider) {
			lambda.StartHandler(WrapHandler(customBytes, provider, lambda.WithContextValue(contextOptionKey{}, "from-option")))
		},
	},
}

var errSyntheticFailure = errors.New("synthetic failure")

func contextOptionHandler(ctx context.Context) (string, error) {
	return contextOptionValue(ctx), nil
}

var customBytes = handlerFunc(func(ctx context.Context, _ []byte) ([]byte, error) {
	return []byte(`{"custom":"` + contextOptionValue(ctx) + `"}`), nil
})

func sdkCase(handler any, options ...lambda.Option) struct{ plain, wrapped func(Provider) } {
	return struct{ plain, wrapped func(Provider) }{
		plain: func(Provider) { lambda.StartWithOptions(handler, options...) },
		wrapped: func(provider Provider) {
			lambda.StartHandler(WrapHandler(lambda.NewHandler(handler), provider, options...))
		},
	}
}

// TestRuntimeLoopChild is the Lambda process started by runThroughRuntime.
func TestRuntimeLoopChild(t *testing.T) {
	name := os.Getenv(runtimeCaseEnv)
	if name == "" {
		t.Skip("runtime child process only")
	}
	caseName, wrapped := strings.CutSuffix(name, "/wrapped")
	caseName = strings.TrimSuffix(caseName, "/plain")
	start, ok := runtimeCases[caseName]
	if !ok {
		t.Fatalf("unknown runtime case %q", caseName)
	}
	if wrapped {
		start.wrapped(sdktrace.NewTracerProvider())
	} else {
		start.plain(nil)
	}
}

type runtimeResponse struct {
	endpoint, contentType, body string
}

// runThroughRuntime serves one invocation to a child process running the
// named handler and returns what the runtime posted back.
func runThroughRuntime(t *testing.T, name, event string) runtimeResponse {
	t.Helper()
	var (
		mu     sync.Mutex
		served bool
		got    runtimeResponse
	)
	done := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		switch {
		case strings.HasSuffix(r.URL.Path, "/invocation/next") && !served:
			served = true
			w.Header().Set("Lambda-Runtime-Aws-Request-Id", "synthetic-request")
			w.Header().Set("Lambda-Runtime-Deadline-Ms", strconv.FormatInt(time.Now().Add(10*time.Second).UnixMilli(), 10))
			w.Header().Set("Lambda-Runtime-Invoked-Function-Arn", "arn:aws:lambda:us-east-1:123456789012:function:example")
			_, _ = io.WriteString(w, event)
		case strings.HasSuffix(r.URL.Path, "/response"), strings.HasSuffix(r.URL.Path, "/error"):
			body, _ := io.ReadAll(r.Body)
			got = runtimeResponse{endpoint: r.URL.Path[strings.LastIndex(r.URL.Path, "/")+1:], contentType: r.Header.Get("Content-Type"), body: string(body)}
			w.WriteHeader(http.StatusAccepted)
			close(done)
		default:
			// A second /next ends the child's runtime loop.
			w.WriteHeader(http.StatusInternalServerError)
		}
	}))
	defer server.Close()

	cmd := exec.Command(os.Args[0], "-test.run=^TestRuntimeLoopChild$")
	cmd.Env = append(os.Environ(),
		runtimeCaseEnv+"="+name,
		"AWS_LAMBDA_RUNTIME_API="+strings.TrimPrefix(server.URL, "http://"),
		"AWS_LAMBDA_FUNCTION_NAME=example",
	)
	if err := cmd.Start(); err != nil {
		t.Fatalf("start runtime child: %v", err)
	}
	t.Cleanup(func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	})
	select {
	case <-done:
	case <-time.After(20 * time.Second):
		t.Fatalf("%s: runtime posted no response", name)
	}
	mu.Lock()
	defer mu.Unlock()
	return got
}

func TestWrapHandlerPreservesRuntimeResponse(t *testing.T) {
	for _, tc := range []struct {
		name, event, contentType string
	}{
		{"json", `{}`, "application/json"},
		{"no-result", `{}`, "application/json"},
		{"reader", `{}`, "application/octet-stream"},
		{"function-url-stream", functionURLEvent, "application/vnd.awslambda.http-integration-response"},
		{"function-url-detect-option", functionURLEvent, "application/vnd.awslambda.http-integration-response"},
		{"context-option", `{}`, "application/json"},
		{"nil-provider-context-option", `{}`, "application/json"},
		{"error", `{}`, "application/json"},
		{"custom-bytes", `{}`, "application/octet-stream"},
		{"custom-bytes-context-option", `{}`, "application/octet-stream"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			plain := runThroughRuntime(t, tc.name+"/plain", tc.event)
			wrapped := runThroughRuntime(t, tc.name+"/wrapped", tc.event)
			if wantEndpoint := map[bool]string{true: "error", false: "response"}[tc.name == "error"]; plain.endpoint != wantEndpoint {
				t.Fatalf("unexpected aws-lambda-go baseline endpoint %q", plain.endpoint)
			}
			if plain.contentType != tc.contentType {
				t.Fatalf("unexpected aws-lambda-go baseline %q", plain.contentType)
			}
			if strings.HasSuffix(tc.name, "-option") && !strings.Contains(plain.body, "text/html") && !strings.Contains(plain.body, "from-option") {
				t.Fatalf("baseline did not apply the Lambda option: %q", plain.body)
			}
			if wrapped != plain {
				t.Fatalf("wrapper changed the runtime response:\n plain   %q %q\n wrapped %q %q", plain.contentType, plain.body, wrapped.contentType, wrapped.body)
			}
		})
	}
}
