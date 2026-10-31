package injector

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

type byteCounter struct {
	reader io.Reader
	n      int
}

func (c *byteCounter) Read(p []byte) (int, error) {
	n, err := c.reader.Read(p)
	c.n += n
	return n, err
}

func TestOverloadUIDProbeIsBounded(t *testing.T) {
	for _, tc := range []struct{ name, body, want string }{
		{"normal", `{"apiVersion":"admission.k8s.io/v1","request":{"uid":"synthetic-uid","object":{"padding":"` + strings.Repeat("x", MaxRequestBytes) + `"}}}`, "synthetic-uid"},
		{"reordered", `{"ignored":true,"request":{"kind":{},"uid":"synthetic-uid"}}`, "synthetic-uid"},
		{"unreadable", `{"request":{"object":{"padding":"` + strings.Repeat("x", MaxRequestBytes) + `"},"uid":"late"}}`, ""},
		{"malformed", `{"request":null}`, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			reader := &byteCounter{reader: strings.NewReader(tc.body)}
			if got := overloadUID(reader); string(got) != tc.want {
				t.Fatalf("UID=%q; want %q", got, tc.want)
			}
			if reader.n > 64<<10 {
				t.Fatal("overload retained/read a full Pod outside admission budget")
			}
		})
	}
}

func TestAdmissionCanceledRequestDoesNotMutate(t *testing.T) {
	review := admissionRequest(t)
	data, _ := json.Marshal(review)
	r := httptest.NewRequest(http.MethodPost, "/mutate", bytes.NewReader(data))
	ctx, cancel := context.WithCancel(r.Context())
	cancel()
	w := httptest.NewRecorder()
	Handler{Config: testConfig()}.ServeHTTP(w, r.WithContext(ctx))
	var out struct {
		Response struct {
			Allowed bool
			Patch   []byte
		}
	}
	if json.Unmarshal(w.Body.Bytes(), &out) != nil || !out.Response.Allowed || len(out.Response.Patch) != 0 {
		t.Fatal("canceled request mutated/rejected Pod")
	}
}

func TestAdmissionSlowBodyDeadline(t *testing.T) {
	// Real loopback HTTP verifies ResponseController deadlines, not just mocked
	// contexts. A slow/malformed exchange is ignored by the API's failure policy.
	server := httptest.NewServer(Handler{Config: testConfig()})
	defer server.Close()
	reader, writer := io.Pipe()
	defer writer.Close()
	client := server.Client()
	client.Timeout = 3 * time.Second
	started := time.Now()
	response, err := client.Post(server.URL, "application/json", reader)
	if err == nil {
		response.Body.Close()
	}
	if time.Since(started) > 2*time.Second {
		t.Fatal("slow body escaped 1s admission read deadline")
	}
}
