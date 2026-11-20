package auto

import (
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"

	"golang.org/x/net/http/httpproxy"
)

// Transport rules for the FI API key and secret. Every key-carrying send goes
// through keyTransport (at activation, on the written FI_BASE_URL, and again on
// each request the exporter transport actually sends) before a key header can
// leave the process. No error here echoes a configured value or a key.
//
//  1. Literal loopback (localhost, 127.0.0.1, ::1) may use plaintext.
//  2. Future AGI hosts always use TLS; http:// is refused, the opt-in never applies.
//  3. The host as written must agree with the host Go parsed, else refused.
//  4. Any other plaintext target is refused unless FI_ALLOW_INSECURE_HTTP is
//     exactly "1"; the opt-in warns once per process, without values.
//  5. A plaintext send is never proxied; an https send uses the environment proxy
//     (a CONNECT tunnel, with the host verified against the written name).
//  6. A 3xx answer to a key-carrying send is refused.
var insecureWarning sync.Once

// dialAddress maps the address the transport asks for to the one dialed. It is the identity; tests replace it to
// model a name that resolves somewhere other than loopback, which the dial guard must refuse.
var dialAddress = func(addr string) string { return addr }

var errKeyTransport = errors.New("traceai auto: refusing to send Future AGI keys over this transport")

func refuse(why string) error { return fmt.Errorf("%w (%s)", errKeyTransport, why) }

// writtenHost is the host exactly as written in the URL string: no case,
// numeric-form or trailing-dot normalization. ok is false for a form whose
// reading could differ between parsers (backslash, userinfo, control bytes).
func writtenHost(raw string) (host string, ok bool) {
	i := strings.Index(raw, "://")
	if i < 0 {
		return "", false
	}
	rest := raw[i+3:]
	if end := strings.IndexAny(rest, "/?#"); end >= 0 {
		rest = rest[:end]
	}
	if rest == "" || strings.ContainsAny(rest, "\\@ \t\r\n") {
		return "", false
	}
	for _, r := range rest {
		if r < 0x21 || r == 0x7f {
			return "", false
		}
	}
	if strings.HasPrefix(rest, "[") {
		end := strings.Index(rest, "]")
		if end < 0 {
			return "", false
		}
		return rest[1:end], true
	}
	if c := strings.LastIndex(rest, ":"); c >= 0 {
		rest = rest[:c]
	}
	return rest, true
}

func literalLoopback(host string) bool {
	return host == "localhost" || host == "127.0.0.1" || host == "::1"
}

// futureAGIHost applies the single-trailing-dot and case rule to the written host.
func futureAGIHost(host string) bool {
	h := strings.ToLower(strings.TrimSuffix(host, "."))
	return h == "futureagi.com" || strings.HasSuffix(h, ".futureagi.com")
}

// keyTransport decides whether FI keys may be sent to written (the target as
// configured) whose parsed form is u.
func keyTransport(written string, u *url.URL) error {
	host, ok := writtenHost(written)
	if !ok || u == nil || u.User != nil {
		return refuse("unreadable target")
	}
	if !strings.EqualFold(host, u.Hostname()) {
		return refuse("written and parsed host differ")
	}
	switch u.Scheme {
	case "https":
		return nil
	case "http":
	default:
		return refuse("unsupported scheme")
	}
	switch {
	case futureAGIHost(host):
		return refuse("Future AGI requires TLS")
	case literalLoopback(host):
		return nil
	case os.Getenv("FI_ALLOW_INSECURE_HTTP") == "1":
		insecureWarning.Do(func() {
			log.Print("traceai auto: FI_ALLOW_INSECURE_HTTP=1 sends Future AGI keys over plaintext HTTP to a non-loopback target")
		})
		return nil
	}
	return refuse("plaintext target; set FI_ALLOW_INSECURE_HTTP=1 only for a trusted private network")
}

// requestTransport applies keyTransport to a request about to be sent.
func requestTransport(req *http.Request) error {
	return keyTransport(req.URL.Scheme+"://"+req.URL.Host, req.URL)
}

// exporterProxy is the exporter transport's proxy choice, read from the environment per request. A plaintext
// request carrying the keys is never proxied: it goes straight to its target. An https request follows the
// environment (HTTPS_PROXY, NO_PROXY), so the keys travel inside TLS, end to end, through a CONNECT tunnel; the
// certificate is still verified against the written host. Go does not read ALL_PROXY.
func exporterProxy(req *http.Request) (*url.URL, error) {
	if req.URL.Scheme != "https" {
		return nil, nil
	}
	return httpproxy.FromEnvironment().ProxyFunc()(req.URL)
}

// checkDialAddress makes the connected peer agree with a loopback claim.
func checkDialAddress(written, addr string) error {
	if !literalLoopback(written) {
		return nil
	}
	h, _, err := net.SplitHostPort(addr)
	if err != nil {
		return refuse("unreadable peer")
	}
	if ip := net.ParseIP(h); ip == nil || !ip.IsLoopback() {
		return refuse("connected peer is not loopback")
	}
	return nil
}
