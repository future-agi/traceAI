package http2

import (
	"context"
	"errors"
	"net/http"
	"testing"
	"time"
)

type contextProbe struct {
	t      *testing.T
	closed bool
	cancel *http.Request
}

func (p *contextProbe) RoundTrip(r *http.Request) (*http.Response, error) {
	if r.Cancel != nil {
		p.t.Error("http.Client selected the extra legacy cancellation timer")
	}
	if _, ok := r.Context().Deadline(); !ok {
		p.t.Error("client deadline not propagated")
	}
	<-r.Context().Done()
	return nil, r.Context().Err()
}
func (p *contextProbe) CloseIdleConnections()         { p.closed = true }
func (p *contextProbe) CancelRequest(r *http.Request) { p.cancel = r }

func TestClientUsesNativeContextDeadline(t *testing.T) {
	probe := &contextProbe{t: t}
	client := &http.Client{Transport: &Transport{RoundTripper: probe}, Timeout: 10 * time.Millisecond}
	r, _ := http.NewRequest("GET", "http://unused.invalid", nil)
	_, err := client.Do(r)
	if !errors.Is(err, context.DeadlineExceeded) || probe.cancel != nil {
		t.Fatalf("native deadline or legacy cancellation changed: %v", err)
	}
}

func TestOptionalTransportMethods(t *testing.T) {
	probe := &contextProbe{t: t}
	transport := &Transport{RoundTripper: probe}
	(&http.Client{Transport: transport}).CloseIdleConnections()
	r, _ := http.NewRequest("GET", "http://unused.invalid", nil)
	transport.CancelRequest(r)
	if !probe.closed || probe.cancel != r {
		t.Fatal("transport optional methods not forwarded")
	}
}
