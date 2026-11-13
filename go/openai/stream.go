package traceaiopenai

import (
	"bytes"
	"context"
	"io"
	"sync"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

// streamBody passes a server-sent-events body through to the SDK unchanged
// while it accumulates the chunks. The span ends exactly once: at "[DONE]",
// at the end of the body, on a read error (including a cancelled context),
// or when the caller closes the stream early.
type streamBody struct {
	rc     io.ReadCloser
	reqCtx context.Context
	span   trace.Span
	tc     TraceConfig
	op     operation

	mu     sync.Mutex
	parser sseParser
	acc    *accumulator
	ended  bool
}

func newStreamBody(reqCtx context.Context, rc io.ReadCloser, span trace.Span, tc TraceConfig, op operation) io.ReadCloser {
	return &streamBody{rc: rc, reqCtx: reqCtx, span: span, tc: tc, op: op, acc: newAccumulator()}
}

func (s *streamBody) Read(p []byte) (int, error) {
	n, err := s.rc.Read(p)
	s.mu.Lock()
	defer s.mu.Unlock()
	if n > 0 && !s.ended {
		s.parser.feed(p[:n], s.onEvent)
	}
	switch {
	case err == io.EOF:
		s.parser.flush(s.onEvent)
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
	// A stream closed before [DONE] without a cancelled context was stopped
	// by the caller; it ends without an error status.
	s.end(s.reqCtx.Err())
	return err
}

func (s *streamBody) onEvent(data []byte) {
	if s.ended {
		return
	}
	if bytes.Equal(bytes.TrimSpace(data), []byte("[DONE]")) {
		s.end(nil)
		return
	}
	if s.op == opResponses {
		if s.acc.addResponseEvent(data) {
			s.end(nil)
		}
	} else {
		s.acc.addChunk(data)
	}
}

// end must be called with s.mu held.
func (s *streamBody) end(err error) {
	if s.ended {
		return
	}
	s.ended = true
	s.span.SetAttributes(s.tc.Filter(s.acc.attributes(s.tc))...)
	switch {
	case err != nil:
		f := failureClass(s.reqCtx, err)
		s.span.SetAttributes(attribute.String(ErrorType, f.errType))
		s.span.SetStatus(codes.Error, f.desc)
	default:
		setResponseError(s.span, s.acc.streamErr, s.tc, s.op)
	}
	s.span.End()
}

// setResponseError handles API-level failures in successful HTTP responses,
// including Responses failed/incomplete results and streaming error events.
func setResponseError(span trace.Span, e *apiError, tc TraceConfig, op operation) {
	if e == nil {
		return
	}
	errType, desc := "response_error", "error in response"
	if op == opChat {
		errType, desc = "stream_error", "error event in stream"
	}
	if safeToken(e.Type) {
		errType = e.Type
	} else if op != opChat && safeToken(e.Code) {
		errType = e.Code
	}
	if safeToken(e.Code) {
		desc += " " + e.Code
	}
	attrs := []attribute.KeyValue{attribute.String(ErrorType, errType)}
	if msg := truncate(e.Message, maxErrorMessage); msg != "" {
		attrs = append(attrs, attribute.String(ErrorMessage, msg))
		if tc.errorMessageAllowed() {
			desc += ": " + msg
		}
	}
	span.SetAttributes(tc.Filter(attrs)...)
	span.SetStatus(codes.Error, desc)
}

// sseParser splits a text/event-stream body into the data payload of each
// event (https://html.spec.whatwg.org/multipage/server-sent-events.html).
type sseParser struct {
	buf     []byte
	data    []byte
	hasData bool
}

func (p *sseParser) feed(b []byte, onEvent func([]byte)) {
	p.buf = append(p.buf, b...)
	for {
		i := bytes.IndexByte(p.buf, '\n')
		if i < 0 {
			break
		}
		line := bytes.TrimSuffix(p.buf[:i], []byte("\r"))
		p.buf = p.buf[i+1:]
		p.line(line, onEvent)
	}
	if len(p.buf) == 0 {
		p.buf = nil
	}
}

func (p *sseParser) line(line []byte, onEvent func([]byte)) {
	if len(line) == 0 {
		p.dispatch(onEvent)
		return
	}
	if line[0] == ':' {
		return
	}
	field, value := line, []byte(nil)
	if j := bytes.IndexByte(line, ':'); j >= 0 {
		field, value = line[:j], bytes.TrimPrefix(line[j+1:], []byte(" "))
	}
	if string(field) == "data" {
		if p.hasData {
			p.data = append(p.data, '\n')
		}
		p.data = append(p.data, value...)
		p.hasData = true
	}
}

func (p *sseParser) dispatch(onEvent func([]byte)) {
	if p.hasData {
		onEvent(p.data)
	}
	p.data = p.data[:0]
	p.hasData = false
}

// flush handles a final event that was not followed by a blank line.
func (p *sseParser) flush(onEvent func([]byte)) {
	if len(p.buf) > 0 {
		p.line(bytes.TrimSuffix(p.buf, []byte("\r")), onEvent)
		p.buf = nil
	}
	p.dispatch(onEvent)
}
