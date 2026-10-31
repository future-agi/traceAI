package auto

import (
	"context"
	"encoding/base64"
	"net/http"
	"net/url"
	"sync"
	"weak"

	"github.com/future-agi/traceAI/go/auto/internal/wire"
)

// Weak keys prevent an application that repeatedly creates short-lived clients
// from retaining every concrete transport for the lifetime of the agent.
// Installation is not a request hot path; prune retired keys on each install.
type nativeCredentialHooks struct {
	mu         sync.Mutex
	transports map[weak.Pointer[http.Transport]]struct{}
}

func observeCredentialHeaders(headers http.Header) {
	_ = guarded(func() error {
		for _, value := range wire.CredentialValues(headers, "") {
			RegisterSecret(value)
		}
		return nil
	})
}

// Install on concrete native transports at initialization/Transport, never while
// requests are running. The native Proxy callback sees the actual request even
// for HTTP/1.1, which has no httptrace request-target callback. CONNECT headers
// likewise bypass WroteHeaderField, so observe their native configuration hooks.
// User callbacks run exactly once, outside our panic guards; their return values,
// errors and panics are unchanged. No connection, body or response is replaced.
//
// An opaque RoundTripper can hide another, uninstrumented native transport. These
// hooks cannot reach it: callers must also instrument that concrete transport.
func (s *agentState) observeNativeTransport(native *http.Transport) {
	hooks := &s.nativeHooks
	hooks.mu.Lock()
	defer hooks.mu.Unlock()
	key := weak.Make(native)
	if _, exists := hooks.transports[key]; exists {
		return
	}
	for key := range hooks.transports {
		if key.Value() == nil {
			delete(hooks.transports, key)
		}
	}
	if hooks.transports == nil {
		hooks.transports = make(map[weak.Pointer[http.Transport]]struct{})
	}
	hooks.transports[key] = struct{}{}
	proxy := native.Proxy
	native.Proxy = func(req *http.Request) (*url.URL, error) {
		var target *url.URL
		var err error
		if proxy != nil {
			target, err = proxy(req)
		}
		if s.active.Load() {
			observeRequestCredentials(req)
			if native.GetProxyConnectHeader == nil {
				observeCredentialHeaders(native.ProxyConnectHeader)
			}
		}
		if s.active.Load() && err == nil && target != nil && target.User != nil {
			// net/http synthesizes this authentication header from proxy URL
			// userinfo, including for CONNECT (which has no header trace hook).
			_ = guarded(func() error {
				password, _ := target.User.Password()
				token := base64.StdEncoding.EncodeToString([]byte(target.User.Username() + ":" + password))
				RegisterSecret(token)
				RegisterSecret("Basic " + token)
				return nil
			})
		}
		return target, err
	}
	if get := native.GetProxyConnectHeader; get != nil {
		native.GetProxyConnectHeader = func(ctx context.Context, proxyURL *url.URL, target string) (http.Header, error) {
			headers, err := get(ctx, proxyURL, target)
			if s.active.Load() && err == nil {
				observeCredentialHeaders(headers)
			}
			return headers, err
		}
	}
}
