package auto_test

import (
	"io"
	"net/http"
	"path/filepath"
	"strings"
	"testing"

	"github.com/future-agi/traceAI/go/auto"
)

// Documented behaviour: while wire capture is on, http.DefaultTransport is the
// agent's wrapper, not an *http.Transport. A checked type assertion takes its
// fallback and an unchecked one panics. The documented workaround, a new
// *http.Transport wrapped with auto.Transport, is still traced.
func TestDefaultTransportTypeWhileCapturing(t *testing.T) {
	rec, _ := setup(t)
	if _, ok := http.DefaultTransport.(*http.Transport); ok {
		t.Fatal("http.DefaultTransport kept its concrete type; update both quick starts and Known limitations")
	}
	panicked := func() (p bool) {
		defer func() { p = recover() != nil }()
		_ = http.DefaultTransport.(*http.Transport).Clone()
		return false
	}()
	if !panicked {
		t.Fatal("an unchecked http.DefaultTransport.(*http.Transport) assertion did not panic")
	}
	transport := &http.Transport{}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: auto.Transport(transport)}
	server := localServer(t, responseJSON, "application/json", 200)
	resp, err := client.Post(server.URL+"/v1/chat/completions", "application/json", strings.NewReader(requestJSON))
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	if len(wires(rec)) != 1 {
		t.Fatalf("the documented workaround lost the wire span: %d", len(wires(rec)))
	}
}

// With FI_WIRE_CAPTURE=false, http.DefaultTransport keeps its concrete type.
func TestWireCaptureOffKeepsDefaultTransportType(t *testing.T) {
	t.Setenv("FI_WIRE_CAPTURE", "false")
	setup(t)
	if _, ok := http.DefaultTransport.(*http.Transport); !ok {
		t.Fatal("FI_WIRE_CAPTURE=false still replaced http.DefaultTransport")
	}
}

// Package initialization already replaces http.DefaultTransport, before any
// request, when FI keys are set and wire capture is on; otherwise the
// concrete type is kept.
func TestArmedDefaultTransportTypeE2E(t *testing.T) {
	bin := filepath.Join(t.TempDir(), "appprovider")
	build := hostCommand("go", "build", "-o", bin, "./testdata/appprovider")
	build.Env = cleanEnv()
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build application %v %s", err, out)
	}
	keys := []string{"FI_API_KEY=test-fi-key", "FI_SECRET_KEY=test-fi-secret"}
	for _, tc := range []struct {
		name   string
		env    []string
		native string
	}{
		{"armed", keys, "false"},
		{"wire-capture-off", append(append([]string(nil), keys...), "FI_WIRE_CAPTURE=false"), "true"},
		{"no-keys", nil, "true"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cmd := hostCommand(bin, "http://127.0.0.1:9", "http://127.0.0.1:9", "transport")
			cmd.Dir = t.TempDir()
			cmd.Env = append(cleanEnv(), tc.env...)
			out, err := cmd.Output()
			if err != nil {
				t.Fatalf("application failed: %v", err)
			}
			if string(out) != tc.native {
				t.Fatalf("http.DefaultTransport is *http.Transport: got %s, want %s", out, tc.native)
			}
		})
	}
}
