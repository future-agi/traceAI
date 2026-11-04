package traceaianthropic

import (
	"context"
	"strings"
	"testing"
	"unicode/utf8"

	"go.opentelemetry.io/otel/trace"
	"go.opentelemetry.io/otel/trace/noop"
)

func noopSpan() trace.Span {
	_, s := noop.NewTracerProvider().Tracer("t").Start(context.Background(), "s")
	return s
}

func TestCapValueKeepsValidUTF8(t *testing.T) {
	s := strings.Repeat("a", maxValueBytes-1) + "é" + "tail"
	got, cut := capValue(s)
	if !cut || !utf8.ValidString(got) || len(got) > maxValueBytes {
		t.Fatalf("cut=%v valid=%v len=%d", cut, utf8.ValidString(got), len(got))
	}
	if v, cut := capValue("short"); cut || v != "short" {
		t.Fatal("short value changed")
	}
}

func TestStreamAccumulatorSplitsAcrossReads(t *testing.T) {
	s := &streamBody{c: &call{in: &instrumentor{}}}
	s.acc.blocks = map[int]*blockAcc{}
	data := "event: message_start\r\ndata: {\"type\":\"message_start\",\"message\":{\"id\":\"m\",\"model\":\"x\",\"usage\":{\"input_tokens\":3}}}\r\n\r\n" +
		": comment\n" +
		"event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n" +
		"event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"héllo\"}}\n\n"
	for i := 0; i < len(data); i += 7 { // feed in small, rune-splitting pieces
		end := min(i+7, len(data))
		s.feed([]byte(data[i:end]))
	}
	m := s.acc.message()
	if m.ID != "m" || len(m.Content) != 1 || !strings.Contains(string(m.Content[0]), "héllo") {
		t.Fatalf("%+v %s", m, m.Content)
	}
	if *m.Usage.InputTokens != 3 {
		t.Fatal("usage lost")
	}
}

func TestStreamOverTheCapIsMarkedTruncated(t *testing.T) {
	s := &streamBody{c: &call{in: &instrumentor{}, span: noopSpan()}}
	s.acc.blocks = map[int]*blockAcc{}
	big := strings.Repeat("x", maxValueBytes+10)
	for _, ev := range []string{
		`{"type":"message_start","message":{"id":"m","model":"x"}}`,
		`{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}`,
		`{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"` + big + `"}}`,
	} {
		s.acc.apply([]byte(ev))
	}
	if !s.acc.cut {
		t.Fatal("not marked cut")
	}
	if m := s.acc.message(); strings.Contains(string(m.Content[0]), big) {
		t.Error("kept text over the cap")
	}
}
