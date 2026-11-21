package auto

import (
	"context"
	"errors"
	"net"
	"net/http"
	"syscall"
	"time"
)

// The OTel endpoint option discards URL queries. Restore the configured raw
// query only on our dedicated exporter transport (also on retry requests),
// preserving escaping, duplicate parameters and CloseIdleConnections ownership.
type collectorTransport struct {
	*http.Transport
	rawQuery   string
	forceQuery bool
}

func (t *collectorTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	if err := requestTransport(req); err != nil {
		// A RoundTripper closes the request body even when it refuses to send.
		if req.Body != nil {
			_ = req.Body.Close()
		}
		return nil, err
	}
	outgoing := req.Clone(req.Context())
	outgoing.URL.RawQuery = t.rawQuery
	outgoing.URL.ForceQuery = t.forceQuery
	resp, err := t.Transport.RoundTrip(outgoing)
	if err == nil && resp.StatusCode >= 300 && resp.StatusCode < 400 {
		_ = resp.Body.Close()
		return nil, refuse("redirect answered a key-carrying send")
	}
	return resp, err
}

// Use net/http's standard transport defaults, not the mutable global transport
// (which can contain application instrumentation with no unwrapping API).
func exporterClient(rawQuery string, forceQuery bool) *http.Client {
	return &http.Client{
		Transport: &collectorTransport{
			Transport: &http.Transport{
				Proxy:                 exporterProxy,
				DialContext:           dialChecked,
				ForceAttemptHTTP2:     true,
				MaxIdleConns:          100,
				IdleConnTimeout:       90 * time.Second,
				TLSHandshakeTimeout:   10 * time.Second,
				ExpectContinueTimeout: time.Second,
			},
			rawQuery: rawQuery, forceQuery: forceQuery,
		},
		Timeout: 5 * time.Second,
		// FI headers are not standard Authorization headers: net/http would
		// otherwise forward them even across hosts. A collector is an endpoint,
		// not a browser; decline all redirects, including HTTPS downgrades.
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
}

// dialChecked connects like the default dialer, but a loopback claim in the
// written host must be matched by the connected peer.
func dialChecked(ctx context.Context, network, addr string) (net.Conn, error) {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return nil, refuse("unreadable target")
	}
	d := &net.Dialer{Timeout: 30 * time.Second, KeepAlive: 30 * time.Second, Control: func(_, peer string, _ syscall.RawConn) error { return checkDialAddress(host, peer) }}
	conn, err := d.DialContext(ctx, network, dialAddress(addr))
	if errors.Is(err, errKeyTransport) {
		// The dialer's own error names the peer it refused; the refusal says only that the peer was not loopback.
		return nil, refuse("connected peer is not loopback")
	}
	return conn, err
}
