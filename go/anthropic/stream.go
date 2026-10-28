package traceaianthropic

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"sort"
	"strings"
	"sync"
)

// maxLineBytes is the longest server-sent-event line that the stream body
// parses. A longer line still reaches the application unchanged; the span
// just does not read it.
const maxLineBytes = 4 << 20

// streamBody is the body of a streaming Messages response. It hands every byte
// to the application unchanged, follows the server-sent events, and records the
// assembled message on the span. One stream is one span: no span is opened per
// event.
//
// For a span that the middleware opened, the span ends when the body ends:
// at the end of the stream, on a read error, when the application closes the
// body, or when the request's context is canceled, whichever comes first.
// anthropic-sdk-go ends its own span itself.
type streamBody struct {
	rc io.ReadCloser
	c  *call

	mu       sync.Mutex
	line     []byte
	skipping bool
	event    string
	data     []byte
	acc      streamAccumulator
	recorded bool
	done     bool
	stop     func() bool
}

func newStreamBody(ctx context.Context, rc io.ReadCloser, c *call) *streamBody {
	s := &streamBody{rc: rc, c: c}
	s.acc.blocks = map[int]*blockAcc{}
	if c.own {
		// A context canceled while the application is not reading still ends the span.
		s.stop = context.AfterFunc(ctx, func() { s.finish(ctx.Err()) })
	}
	return s
}

func (s *streamBody) Read(p []byte) (int, error) {
	n, err := s.rc.Read(p)
	if n > 0 {
		s.feed(p[:n])
	}
	switch {
	case err == io.EOF:
		s.finish(nil)
	case err != nil:
		s.finish(err)
	}
	return n, err
}

// Close ends the span as canceled when the stream did not reach its end.
func (s *streamBody) Close() error {
	s.finish(context.Canceled)
	return s.rc.Close()
}

// Unwrap returns the body that this one wraps.
func (s *streamBody) Unwrap() io.ReadCloser { return s.rc }

func (s *streamBody) feed(b []byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.done {
		return
	}
	for len(b) > 0 {
		i := bytes.IndexByte(b, '\n')
		if i < 0 {
			s.appendLine(b)
			return
		}
		s.appendLine(b[:i])
		b = b[i+1:]
		if !s.skipping {
			s.handleLine(bytes.TrimSuffix(s.line, []byte("\r")))
		}
		s.line = s.line[:0]
		s.skipping = false
	}
}

func (s *streamBody) appendLine(b []byte) {
	if s.skipping {
		return
	}
	if len(s.line)+len(b) > maxLineBytes {
		s.skipping = true
		s.line = s.line[:0]
		return
	}
	s.line = append(s.line, b...)
}

func (s *streamBody) handleLine(line []byte) {
	switch {
	case len(line) == 0:
		s.dispatch()
	case line[0] == ':':
		// A comment.
	case bytes.HasPrefix(line, []byte("event:")):
		s.event = strings.TrimSpace(string(line[len("event:"):]))
	case bytes.HasPrefix(line, []byte("data:")):
		d := bytes.TrimPrefix(line[len("data:"):], []byte(" "))
		if len(s.data) > 0 {
			s.data = append(s.data, '\n')
		}
		s.data = append(s.data, d...)
	}
}

func (s *streamBody) dispatch() {
	data := s.data
	s.event, s.data = "", nil
	if len(data) == 0 {
		return
	}
	s.acc.apply(data)
	if s.acc.stopped && !s.recorded {
		// Record at message_stop, before the application can close the body:
		// anthropic-sdk-go ends its own span when the body is closed.
		s.record()
	}
}

func (s *streamBody) record() {
	if !s.acc.started {
		return
	}
	s.recorded = true
	m := s.acc.message()
	raw, ok := marshalString(m)
	if !ok {
		raw = ""
	}
	s.c.recordMessage(m, raw)
	if s.acc.cut {
		// The stream had more text than the span keeps.
		s.c.set(keyAnthropicTruncated.Bool(true))
	}
}

// finish records what the stream had and, for a span that the middleware
// opened, sets its status and ends it. err is nil at the end of the body.
func (s *streamBody) finish(err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.done {
		return
	}
	s.done = true
	if s.stop != nil {
		s.stop()
	}
	if !s.recorded {
		s.record()
	}
	if !s.c.own {
		return
	}
	switch {
	case s.acc.errType != "":
		s.c.fail(s.acc.errType)
	case err == nil && s.acc.stopped:
		s.c.end()
	case err == nil:
		// The server ended the stream before message_stop.
		s.c.fail("api_error")
	case errors.Is(err, context.Canceled) && s.acc.stopped:
		s.c.end()
	default:
		s.c.fail(errorType(err))
	}
}

// streamAccumulator assembles a Message from the events of a stream.
type streamAccumulator struct {
	started bool
	stopped bool
	errType string
	msg     message
	usage   usage
	blocks  map[int]*blockAcc
	size    int
	cut     bool
}

type blockAcc struct {
	typ      string
	text     strings.Builder
	thinking strings.Builder
	id, name string
	input    json.RawMessage
	partial  strings.Builder
}

type streamEvent struct {
	Type         string          `json:"type"`
	Index        int             `json:"index"`
	Message      *message        `json:"message"`
	ContentBlock json.RawMessage `json:"content_block"`
	Delta        struct {
		Type         string  `json:"type"`
		Text         string  `json:"text"`
		Thinking     string  `json:"thinking"`
		PartialJSON  string  `json:"partial_json"`
		StopReason   *string `json:"stop_reason"`
		StopSequence *string `json:"stop_sequence"`
	} `json:"delta"`
	Usage *usage `json:"usage"`
	Error struct {
		Type string `json:"type"`
	} `json:"error"`
}

func (a *streamAccumulator) apply(data []byte) {
	var ev streamEvent
	if json.Unmarshal(data, &ev) != nil {
		return
	}
	switch ev.Type {
	case "message_start":
		if ev.Message == nil {
			return
		}
		a.started = true
		a.msg.ID, a.msg.Type, a.msg.Role, a.msg.Model = ev.Message.ID, ev.Message.Type, ev.Message.Role, ev.Message.Model
		a.usage.merge(ev.Message.Usage)
	case "content_block_start":
		var b contentBlock
		if json.Unmarshal(ev.ContentBlock, &b) != nil {
			return
		}
		acc := &blockAcc{typ: b.Type, id: b.ID, name: b.Name}
		a.add(len(b.Text) + len(b.Thinking))
		if !a.cut {
			acc.text.WriteString(b.Text)
			acc.thinking.WriteString(b.Thinking)
		}
		if in := bytes.TrimSpace(b.Input); len(in) > 0 && !bytes.Equal(in, []byte("{}")) {
			acc.input = b.Input
		}
		a.blocks[ev.Index] = acc
	case "content_block_delta":
		acc := a.blocks[ev.Index]
		if acc == nil {
			return
		}
		a.add(len(ev.Delta.Text) + len(ev.Delta.Thinking) + len(ev.Delta.PartialJSON))
		if a.cut {
			return
		}
		switch ev.Delta.Type {
		case "text_delta":
			acc.text.WriteString(ev.Delta.Text)
		case "thinking_delta":
			acc.thinking.WriteString(ev.Delta.Thinking)
		case "input_json_delta":
			acc.partial.WriteString(ev.Delta.PartialJSON)
		}
	case "message_delta":
		if ev.Delta.StopReason != nil {
			a.msg.StopReason = ev.Delta.StopReason
		}
		if ev.Delta.StopSequence != nil {
			a.msg.StopSequence = ev.Delta.StopSequence
		}
		a.usage.merge(ev.Usage)
	case "message_stop":
		a.stopped = true
	case "error":
		a.errType = ev.Error.Type
		if a.errType == "" {
			a.errType = "api_error"
		}
	}
}

func (a *streamAccumulator) add(n int) {
	a.size += n
	if a.size > maxValueBytes {
		a.cut = true
	}
}

// message returns the message that the events so far describe, in the shape
// of a non-streaming response.
func (a *streamAccumulator) message() *message {
	m := a.msg
	if m.Type == "" {
		m.Type = "message"
	}
	u := a.usage
	m.Usage = &u
	idx := make([]int, 0, len(a.blocks))
	for i := range a.blocks {
		idx = append(idx, i)
	}
	sort.Ints(idx)
	m.Content = make([]json.RawMessage, 0, len(idx))
	for _, i := range idx {
		if raw, ok := a.blocks[i].json(); ok {
			m.Content = append(m.Content, raw)
		}
	}
	return &m
}

func (b *blockAcc) json() (json.RawMessage, bool) {
	var v any
	switch b.typ {
	case "text":
		v = textBlock{Type: "text", Text: b.text.String()}
	case "thinking":
		v = struct {
			Type     string `json:"type"`
			Thinking string `json:"thinking"`
		}{"thinking", b.thinking.String()}
	case "tool_use", "server_tool_use", "mcp_tool_use":
		input := b.input
		if p := b.partial.String(); p != "" {
			if json.Valid([]byte(p)) {
				input = json.RawMessage(p)
			} else {
				q, _ := json.Marshal(p)
				input = q
			}
		}
		if len(input) == 0 {
			input = json.RawMessage("{}")
		}
		v = struct {
			Type  string          `json:"type"`
			ID    string          `json:"id"`
			Name  string          `json:"name"`
			Input json.RawMessage `json:"input"`
		}{b.typ, b.id, b.name, input}
	default:
		v = typeOnly{Type: b.typ}
	}
	raw, err := json.Marshal(v)
	return raw, err == nil
}
