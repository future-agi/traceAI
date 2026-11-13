package auto_test

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/future-agi/traceAI/go/auto"
	"go.opentelemetry.io/otel/attribute"
)

// The session id an application sets on purpose with ContextWithSessionID is
// exported as its value, although "session" is a credential marker in the
// privacy catalog. Known secrets and credential-shaped values inside it are
// still redacted, and a session.id from any other source keeps its name-based
// redaction.
func TestContextSessionIDKeepsItsValue(t *testing.T) {
	rec, _ := setup(t)
	server := localServer(t, responseJSON, "application/json", 200)
	auto.RegisterSecret("registered-session-secret")
	for _, tc := range []struct {
		name     string
		ctx      context.Context
		want     string
		withheld string
	}{
		{"typed", auto.ContextWithSessionID(context.Background(), "checkout-session-42"), "checkout-session-42", ""},
		{"known-secret", auto.ContextWithSessionID(context.Background(), "checkout registered-session-secret"), "", "registered-session-secret"},
		{"credential-shape", auto.ContextWithSessionID(context.Background(), "Bearer abcdefghijkl"), "", "abcdefghijkl"},
		{"other-source", auto.ContextWithAttributes(context.Background(), attribute.String("session.id", "checkout-session-42")), "<redacted>", "checkout-session-42"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			before := len(wires(rec))
			req, _ := http.NewRequestWithContext(tc.ctx, "POST", server.URL+"/v1/chat/completions", strings.NewReader(requestJSON))
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			spans := wires(rec)
			if len(spans) != before+1 {
				t.Fatalf("want one new wire span, got %d", len(spans)-before)
			}
			got, _ := value(spans[len(spans)-1], "session.id").(string)
			if tc.want != "" && got != tc.want {
				t.Fatalf("session.id = %q, want %q", got, tc.want)
			}
			if got == "" || tc.withheld != "" && strings.Contains(got, tc.withheld) {
				t.Fatalf("session.id = %q must be present without %q", got, tc.withheld)
			}
		})
	}
}
