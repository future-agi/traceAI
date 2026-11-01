package traceaigenai

import (
	"bytes"
	"context"
	"io"
	"sync"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

// streamBody passes a streamGenerateContent body (server-sent events) through
// to the SDK unchanged while it accumulates the chunks. The span ends exactly
// once: at the end of the body, on a read error (including a cancelled
// context), or when the SDK closes the body, which it does when the caller's
// range loop finishes or breaks.
type streamBody struct {
	rc      io.ReadCloser
	reqCtx  context.Context
	span    trace.Span
	tc      TraceConfig
	secrets []string

	mu     sync.Mutex
	parser blockParser
	acc    *accumulator
	ended  bool
}

func newStreamBody(reqCtx context.Context, rc io.ReadCloser, span trace.Span, tc TraceConfig, secrets []string) io.ReadCloser {
	if rc == nil {
		rc = io.NopCloser(bytes.NewReader(nil))
	}
	return &streamBody{rc: rc, reqCtx: reqCtx, span: span, tc: tc, secrets: secrets, acc: newAccumulator()}
}

func (s *streamBody) Read(p []byte) (int, error) {
	n, err := s.rc.Read(p)
	s.mu.Lock()
	defer s.mu.Unlock()
	if n > 0 && !s.ended {
		s.parser.feed(p[:n], s.onBlock)
	}
	switch {
	case err == io.EOF:
		s.parser.flush(s.onBlock)
		s.end(nil)
	case err != nil:
		s.end(err)
	}
	return n, err
}

func (s *streamBody) Close() error {
	err := s.rc.Close()
	s.mu.Lock()
	defer s.mu.Unlock()
	// A stream closed before its end without a cancelled context was stopped
	// by the caller; it ends without an error status.
	s.end(s.reqCtx.Err())
	return err
}

// onBlock handles one event the way the SDK's iterateResponseStream does: a
// "data:" block is a GenerateContentResponse chunk; any other block is read
// as {"error": {...}}.
func (s *streamBody) onBlock(block []byte) {
	if s.ended || len(block) == 0 {
		return
	}
	prefix, data, _ := bytes.Cut(block, []byte(":"))
	if string(prefix) == "data" {
		s.acc.addResponse(data)
		return
	}
	if e := parseAPIError(block); e.Message != "" || e.Status != "" || e.Code != 0 {
		s.acc.streamErr = &e
	}
}

// end must be called with s.mu held.
func (s *streamBody) end(err error) {
	if s.ended {
		return
	}
	s.ended = true
	s.span.SetAttributes(s.tc.Filter(s.acc.attributes())...)
	switch {
	case err != nil:
		f := failureClass(s.reqCtx, err)
		s.span.SetAttributes(attribute.String(ErrorType, f.errType))
		s.span.SetStatus(codes.Error, f.desc)
	case s.acc.streamErr != nil:
		e := s.acc.streamErr
		errType, desc := "stream_error", "error in stream"
		if safeToken(e.Status) {
			errType = e.Status
			desc += " " + e.Status
		}
		attrs := []attribute.KeyValue{attribute.String(ErrorType, errType)}
		if msg := truncate(scrub(e.Message, s.secrets), maxErrorMessage); msg != "" {
			attrs = append(attrs, attribute.String(ErrorMessage, msg))
			if s.tc.errorMessageAllowed() {
				desc += ": " + msg
			}
		}
		s.span.SetAttributes(s.tc.Filter(attrs)...)
		s.span.SetStatus(codes.Error, desc)
	}
	s.span.End()
}

// blockParser splits the body into blocks separated by a blank line, exactly
// like the SDK's stream scanner (genai api_client.go scan): "\n\n" first, then
// "\r\n\r\n", and a final unterminated block at the end of the body. A
// trailing "\r" is dropped from each block.
type blockParser struct {
	buf []byte
}

func (p *blockParser) feed(b []byte, onBlock func([]byte)) {
	p.buf = append(p.buf, b...)
	for {
		if i := bytes.Index(p.buf, []byte("\n\n")); i >= 0 {
			onBlock(dropCR(p.buf[:i]))
			p.buf = p.buf[i+2:]
			continue
		}
		if i := bytes.Index(p.buf, []byte("\r\n\r\n")); i >= 0 {
			onBlock(dropCR(p.buf[:i]))
			p.buf = p.buf[i+4:]
			continue
		}
		break
	}
	if len(p.buf) == 0 {
		p.buf = nil
	}
}

func (p *blockParser) flush(onBlock func([]byte)) {
	if len(p.buf) > 0 {
		onBlock(dropCR(p.buf))
		p.buf = nil
	}
}

func dropCR(b []byte) []byte {
	if n := len(b); n > 0 && b[n-1] == '\r' {
		return b[:n-1]
	}
	return b
}
