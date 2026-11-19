// Package http2 supplies a context-capability adapter, not an HTTP/2 protocol
// implementation. net/http has no public marker for context-aware RoundTripper
// wrappers. On the supported Go toolchain its knownRoundTripperImpl recognizes
// the qualified type name "*http2.Transport" (regardless of import path).
//
// Preserve that name: it keeps http.Client on the SAME cancellation/timer path
// as the native transport. Filtering CancelRequest or Request.Cancel after the
// client has selected its legacy path cannot restore parity: even then the two
// timers race and change Client.Timeout error annotations. Regression tests
// compare actual native HTTP/1.1 and HTTP/2 deadlines with an unwrapped baseline.
package http2

import "net/http"

// Transport must wrap only transports known to honor Request.Context. Custom
// and legacy transports must not use this adapter. No request/body/context is
// changed here, and legacy cancellation remains available when explicitly used.
type Transport struct{ http.RoundTripper }

func (t *Transport) CloseIdleConnections() {
	if closer, ok := t.RoundTripper.(interface{ CloseIdleConnections() }); ok {
		closer.CloseIdleConnections()
	}
}

func (t *Transport) CancelRequest(req *http.Request) {
	if canceler, ok := t.RoundTripper.(interface{ CancelRequest(*http.Request) }); ok {
		canceler.CancelRequest(req)
	}
}
