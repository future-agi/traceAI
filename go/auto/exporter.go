package auto

import (
	"net"
	"net/http"
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
	outgoing := req.Clone(req.Context())
	outgoing.URL.RawQuery = t.rawQuery
	outgoing.URL.ForceQuery = t.forceQuery
	return t.Transport.RoundTrip(outgoing)
}

// Use net/http's standard transport defaults, not the mutable global transport
// (which can contain application instrumentation with no unwrapping API).
func exporterClient(rawQuery string, forceQuery bool) *http.Client {
	return &http.Client{
		Transport: &collectorTransport{
			Transport: &http.Transport{
				Proxy:                 http.ProxyFromEnvironment,
				DialContext:           (&net.Dialer{Timeout: 30 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
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
