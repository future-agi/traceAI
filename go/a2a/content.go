package traceaia2a

import (
	"context"
	"encoding/json"
	"errors"
	"net/url"
	"strings"
	"unicode/utf8"

	"github.com/a2aproject/a2a-go/v2/a2a"
)

// maxContentLen caps input.value, output.value and error.message, in bytes.
const maxContentLen = 32_000

// truncatedSuffix marks a value cut at maxContentLen.
const truncatedSuffix = "...[truncated]"

// partsText returns the text of the parts, one part per line: text parts as
// they are, data parts as JSON. File parts (raw bytes or a URL) are never
// copied: bytes can be large and URLs can carry signed tokens.
func partsText(parts a2a.ContentParts) string {
	var out []string
	for _, p := range parts {
		if p == nil {
			continue
		}
		switch c := p.Content.(type) {
		case a2a.Text:
			if c != "" {
				out = append(out, string(c))
			}
		case a2a.Data:
			if b, err := json.Marshal(c.Value); err == nil && len(b) > 0 && string(b) != "null" {
				out = append(out, string(b))
			}
		}
	}
	return strings.Join(out, "\n")
}

// partsKind returns the kind of the first part, in the gen_ai.a2a.artifact.type
// vocabulary of Python traceai-a2a: text, file or data.
func partsKind(parts a2a.ContentParts) string {
	for _, p := range parts {
		if p == nil {
			continue
		}
		switch p.Content.(type) {
		case a2a.Text:
			return "text"
		case a2a.Raw, a2a.URL:
			return "file"
		case a2a.Data:
			return "data"
		}
	}
	return ""
}

// truncate cuts s to maxContentLen bytes on a rune boundary.
func truncate(s string) string {
	if len(s) <= maxContentLen {
		return s
	}
	cut := maxContentLen - len(truncatedSuffix)
	for cut > 0 && !utf8.RuneStart(s[cut]) {
		cut--
	}
	return s[:cut] + truncatedSuffix
}

// sanitizeURL returns the agent URL without user info, query or fragment, or
// "" when it cannot be parsed. Credentials in the URL never reach a span.
func sanitizeURL(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	u, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	if u.Host == "" {
		// Not scheme://host (for example a gRPC "user:pass@host:port"
		// target, which url.Parse reads as an opaque URL). Cut by hand.
		s := raw
		if i := strings.IndexAny(s, "?#"); i >= 0 {
			s = s[:i]
		}
		if i := strings.LastIndex(s, "@"); i >= 0 {
			s = s[i+1:]
		}
		return s
	}
	u.User = nil
	u.RawQuery = ""
	u.ForceQuery = false
	u.Fragment = ""
	u.RawFragment = ""
	return u.String()
}

// protocolErrors are the A2A protocol errors a2a-go returns, checked when an
// error is not an *a2a.Error.
var protocolErrors = []error{
	a2a.ErrParseError, a2a.ErrInvalidRequest, a2a.ErrMethodNotFound, a2a.ErrInvalidParams,
	a2a.ErrInternalError, a2a.ErrServerError, a2a.ErrTaskNotFound, a2a.ErrTaskNotCancelable,
	a2a.ErrPushNotificationNotSupported, a2a.ErrUnsupportedOperation, a2a.ErrUnsupportedContentType,
	a2a.ErrInvalidAgentResponse, a2a.ErrExtendedCardNotConfigured, a2a.ErrExtensionSupportRequired,
	a2a.ErrVersionNotSupported, a2a.ErrUnauthenticated, a2a.ErrUnauthorized,
}

// classifyError returns error.type for err and, for an error the remote agent
// returned, the agent's message. Context errors are "canceled" or "timeout",
// A2A protocol errors use a2a-go's reason (TASK_NOT_FOUND, ...), anything else
// (connection, HTTP status, decoding) is "transport" and carries no message.
func classifyError(err error) (errType, agentMsg string) {
	switch {
	case err == nil:
		return "", ""
	case errors.Is(err, context.Canceled):
		return "canceled", ""
	case errors.Is(err, context.DeadlineExceeded):
		return "timeout", ""
	}
	var ae *a2a.Error
	if errors.As(err, &ae) {
		return a2a.ErrorReason(ae), ae.Message
	}
	for _, pe := range protocolErrors {
		if errors.Is(err, pe) {
			return a2a.ErrorReason(pe), ""
		}
	}
	return "transport", ""
}
