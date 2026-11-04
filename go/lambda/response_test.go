package traceailambda

import (
	"bytes"
	"context"
	"errors"
	"io"
	"strings"
	"testing"

	"github.com/aws/aws-lambda-go/lambda"
)

// A raw response can release its metadata when the SDK closes it. The Runtime
// API reads ContentType before reading or closing the original response.
type closingResponse struct {
	data   *strings.Reader
	closed bool
}

func (r *closingResponse) Read(p []byte) (int, error) { return r.data.Read(p) }
func (r *closingResponse) Close() error               { r.closed = true; return nil }
func (r *closingResponse) ContentType() string {
	if r.closed {
		return ""
	}
	return "application/vnd.awslambda.http-integration-response"
}

// The SDK chooses JSON for this reader on its first (and only) encoding pass.
// MIME detection must not call application serialization again.
type singleMarshalResponse struct{ calls int }

func (r *singleMarshalResponse) Read([]byte) (int, error) { return 0, io.EOF }
func (r *singleMarshalResponse) MarshalJSON() ([]byte, error) {
	r.calls++
	if r.calls > 1 {
		// Make a repeat observable through the real runtime, not only Invoke.
		panic("encoder already consumed")
	}
	return []byte(`{"answer":"ok"}`), nil
}

type jsonFieldResponse struct {
	io.Reader `json:"-"`
	Answer    string `json:"answer"`
}

type rawMarshalResponse struct {
	*strings.Reader
	calls int
}

func (r *rawMarshalResponse) MarshalJSON() ([]byte, error) {
	r.calls++
	return nil, errors.New("raw response only")
}

func init() {
	runtimeCases["nil-reader"] = sdkCase(func() (io.Reader, error) {
		return (*strings.Reader)(nil), nil
	})
	runtimeCases["nil-typed-reader"] = sdkCase(func() (io.Reader, error) {
		return (*closingResponse)(nil), nil
	})
	runtimeCases["nil-marshaling-reader"] = sdkCase(func() (io.Reader, error) {
		return (*singleMarshalResponse)(nil), nil
	})
	runtimeCases["close-mime"] = sdkCase(func() (io.Reader, error) {
		return &closingResponse{data: strings.NewReader("{\"statusCode\":200}\x00\x00\x00\x00\x00\x00\x00\x00hello")}, nil
	})
	runtimeCases["single-marshal"] = sdkCase(func() (io.Reader, error) {
		return &singleMarshalResponse{}, nil
	})
	runtimeCases["json-field-reader"] = sdkCase(func() (io.Reader, error) {
		return jsonFieldResponse{Reader: strings.NewReader("unused"), Answer: "ok"}, nil
	})
	runtimeCases["json-shaped-raw"] = sdkCase(func() (io.Reader, error) {
		return strings.NewReader(`{"answer":"ok"}`), nil
	})
	runtimeCases["json-shaped-typed"] = sdkCase(func() (io.Reader, error) {
		return &typedResponse{Reader: bytes.NewReader([]byte(`{"answer":"ok"}`)), contentType: "application/x-ndjson"}, nil
	})
}

// A nil pointer is JSON null even when its type implements io.Reader or
// json.Marshaler. Neither the SDK nor MIME capture should call its methods.
func TestRuntimeNilReaderResponse(t *testing.T) {
	for _, name := range []string{"nil-reader", "nil-typed-reader", "nil-marshaling-reader"} {
		t.Run(name, func(t *testing.T) {
			plain := runThroughRuntime(t, name+"/plain", "{}")
			wrapped := runThroughRuntime(t, name+"/wrapped", "{}")
			if plain.endpoint != "response" || plain.contentType != contentTypeJSON || plain.body != "null" {
				t.Fatalf("invalid baseline: %+v", plain)
			}
			if plain != wrapped {
				t.Fatalf("nil reader changed response: plain=%+v wrapped=%+v", plain, wrapped)
			}
		})
	}
}

func TestContentTypeBeforeReaderClose(t *testing.T) {
	plain := runThroughRuntime(t, "close-mime/plain", "{}")
	wrapped := runThroughRuntime(t, "close-mime/wrapped", "{}")
	if plain.endpoint != "response" || plain.contentType != "application/vnd.awslambda.http-integration-response" {
		t.Fatalf("invalid baseline: %+v", plain)
	}
	if plain != wrapped {
		t.Fatalf("reader lifecycle changed response: plain=%+v wrapped=%+v", plain, wrapped)
	}
}

func TestResponseMarshalOnce(t *testing.T) {
	provider, _ := recorderProvider(t)
	response := &singleMarshalResponse{}
	handler := WrapHandler(lambda.NewHandler(func() (io.Reader, error) {
		return response, nil
	}), provider)
	body, err := handler.Invoke(context.Background(), []byte("{}"))
	if err != nil || string(body) != `{"answer":"ok"}` {
		t.Fatalf("response changed: body=%q err=%v", body, err)
	}
	if response.calls != 1 {
		t.Fatalf("MarshalJSON called %d times, want exactly one SDK encoding", response.calls)
	}
}

func TestRawResponseMarshalOnce(t *testing.T) {
	provider, _ := recorderProvider(t)
	response := &rawMarshalResponse{Reader: strings.NewReader("raw")}
	handler := WrapHandler(lambda.NewHandler(func() (io.Reader, error) {
		return response, nil
	}), provider)
	body, err := handler.Invoke(context.Background(), []byte("{}"))
	if err != nil || string(body) != "raw" {
		t.Fatalf("response changed: body=%q err=%v", body, err)
	}
	if response.calls != 1 {
		t.Fatalf("MarshalJSON called %d times, want exactly one SDK encoding attempt", response.calls)
	}
}

// Handler.Invoke hides the SDK's raw-reader versus JSON decision. These
// unsupported hybrid responses retain their bytes, but deliberately use reader
// MIME metadata. Do not silently regain the former double-serialization guess.
func TestRuntimeHybridReaderContract(t *testing.T) {
	for _, name := range []string{"single-marshal", "json-field-reader"} {
		t.Run(name, func(t *testing.T) {
			plain := runThroughRuntime(t, name+"/plain", "{}")
			wrapped := runThroughRuntime(t, name+"/wrapped", "{}")
			if plain.endpoint != "response" || plain.contentType != contentTypeJSON || plain.body != `{"answer":"ok"}` {
				t.Fatalf("invalid baseline: %+v", plain)
			}
			if wrapped.endpoint != plain.endpoint || wrapped.body != plain.body || wrapped.contentType != contentTypeBytes {
				t.Fatalf("documented hybrid-reader limitation changed: plain=%+v wrapped=%+v", plain, wrapped)
			}
		})
	}
}

// Reader bytes that happen to be JSON must not be sniffed as application/json.
func TestRuntimeRawJSONReader(t *testing.T) {
	for _, tc := range []struct{ name, mime string }{
		{"json-shaped-raw", contentTypeBytes},
		{"json-shaped-typed", "application/x-ndjson"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			plain := runThroughRuntime(t, tc.name+"/plain", "{}")
			wrapped := runThroughRuntime(t, tc.name+"/wrapped", "{}")
			if plain.endpoint != "response" || plain.contentType != tc.mime || plain.body != `{"answer":"ok"}` {
				t.Fatalf("invalid baseline: %+v", plain)
			}
			if plain != wrapped {
				t.Fatalf("raw JSON-shaped response changed: plain=%+v wrapped=%+v", plain, wrapped)
			}
		})
	}
}
