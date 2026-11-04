package traceaieino

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/user"
	"regexp"
	"strings"
)

// maxErrorMessage caps error.message.
const maxErrorMessage = 512

// errorType classifies an error for error.type and the span status: canceled
// and timeout for the context, otherwise the Go type of the innermost error
// (for example *openai.APIError), or "error" for a plain errors.New /
// fmt.Errorf value. It never contains the error text.
func errorType(err error) string {
	switch {
	case err == nil:
		return ""
	case errors.Is(err, context.Canceled):
		return "canceled"
	case errors.Is(err, context.DeadlineExceeded):
		return "timeout"
	}
	inner := err
	for {
		next := errors.Unwrap(inner)
		if next == nil {
			break
		}
		inner = next
	}
	switch t := fmt.Sprintf("%T", inner); t {
	case "*errors.errorString", "*fmt.wrapError", "*fmt.wrapErrors", "*errors.joinError", "":
		return "error"
	default:
		return t
	}
}

var (
	reURL      = regexp.MustCompile(`[A-Za-z][A-Za-z0-9+.\-]*://[^\s"'<>]+`)
	reEmail    = regexp.MustCompile(`[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}`)
	reWinPath  = regexp.MustCompile(`[A-Za-z]:\\[^\s"']+`)
	reUnixPath = regexp.MustCompile(`(^|[\s"'(=:])(~?/[^\s"'():,]+)`)
	reIPv4     = regexp.MustCompile(`\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b`)
	reHostPort = regexp.MustCompile(`\b[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9\-]+)*:\d{2,5}\b`)
)

// scrubber turns an error into a one-line message that is safe to export:
// only the first line (a panic's stack trace follows it), with URLs, email
// addresses, file paths, IP addresses, host:port pairs, and this machine's
// host name, user name and home directory replaced by placeholders.
type scrubber struct {
	literals []string
}

func newScrubber() *scrubber {
	s := &scrubber{}
	add := func(v string) {
		if len(v) >= 3 {
			s.literals = append(s.literals, v)
		}
	}
	if h, err := os.Hostname(); err == nil {
		add(h)
		if i := strings.IndexByte(h, '.'); i > 0 {
			add(h[:i])
		}
	}
	if u, err := user.Current(); err == nil {
		add(u.HomeDir)
		add(u.Username)
	}
	add(os.Getenv("HOME"))
	add(os.Getenv("USER"))
	return s
}

func (s *scrubber) message(err error) string {
	if err == nil {
		return ""
	}
	msg := err.Error()
	if i := strings.IndexAny(msg, "\r\n"); i >= 0 {
		msg = msg[:i]
	}
	msg = reURL.ReplaceAllString(msg, "<url>")
	msg = reEmail.ReplaceAllString(msg, "<email>")
	msg = reWinPath.ReplaceAllString(msg, "<path>")
	msg = reUnixPath.ReplaceAllString(msg, "$1<path>")
	msg = reIPv4.ReplaceAllString(msg, "<host>")
	msg = reHostPort.ReplaceAllString(msg, "<host>")
	for _, lit := range s.literals {
		msg = strings.ReplaceAll(msg, lit, "<redacted>")
	}
	msg = strings.TrimSpace(msg)
	if len(msg) > maxErrorMessage {
		msg = truncate(msg, maxErrorMessage)
	}
	return msg
}
