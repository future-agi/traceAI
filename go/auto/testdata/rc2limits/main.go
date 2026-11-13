// A zero-code application: the agent activates from package initialization
// (environment configuration only) and owns the global provider. The program
// uses only net/http and the OpenTelemetry API, plus the documented
// auto.Shutdown flush. Its span limits come from the standard OTEL_* variables.
package main

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/future-agi/traceAI/go/auto"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
)

const pcm = "AQIDBAUGBwgJCgsMDQ4PEBESExQ=" // Headerless PCM: identified only by MIME.
const short = "AQI"                        // Short encoded audio: no length or magic to go on.

// wrapped is catalog-recognized ISO-BMFF base64 ("AAAAEGZ0eXA..." = box size,
// "ftyp") with CR/LF pairs after its first 4 characters, so a value-length
// limit keeps only "AAAA", CR/LF and "EG": no signature byte survives.
func wrapped(limit int) string {
	return "AAAA" + strings.Repeat("\r\n", (limit-6)/2) + "EGZ0eXBpc29tMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAw"
}

// arbitrary puts wrapped media at an arbitrary key and below the id and
// transcript metadata boundaries.
func arbitrary(limit int) []attribute.KeyValue {
	return []attribute.KeyValue{attribute.String("app.arbitrary", wrapped(limit)), attribute.String("input.id.parts.9.arbitrary", wrapped(limit)), attribute.String("app.transcript.parts.83.arbitrary", wrapped(limit))}
}

func main() {
	defer func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = auto.Shutdown(ctx)
	}()
	resp, err := http.Post(os.Args[1]+"/v1/chat/completions", "application/json", strings.NewReader(`{"model":"test-model","messages":[{"role":"user","content":"Hi"}]}`))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	b, err := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if err != nil {
		os.Exit(2)
	}
	_, _ = os.Stdout.Write(b)

	tracer := otel.Tracer("application")
	var attrs, event []attribute.KeyValue
	switch os.Args[2] {
	case "count": // OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT=32: the hint is evicted.
		attrs = append(attrs, attribute.String("app.note", "message 3")) // Not media-capable: survives.
		for i := 0; i < 30; i++ {
			attrs = append(attrs, attribute.String(fmt.Sprintf("gen_ai.input.messages.%d.content", i), fmt.Sprintf("message %d", i)))
		}
		attrs = append(attrs, attribute.String("app.transcript.parts.83.data", short), attribute.String("app.transcript.parts.83.mime_type", "audio/L16"), attribute.String("gen_ai.input.messages.40.parts.0.data", pcm), attribute.String("gen_ai.input.messages.40.parts.0.mime_type", "audio/L16"), attribute.String("input.transcript.parts.0.data", pcm), attribute.String("input.transcript.parts.0.mime_type", "audio/L16"))
		event = []attribute.KeyValue{attribute.String("p.data", pcm), attribute.String("q.transcript.parts.83.data", short), attribute.String("p.f1", "filler text"), attribute.String("p.f2", "filler text"), attribute.String("p.mime_type", "audio/L16"), attribute.String("q.transcript.parts.83.mime_type", "audio/L16")}
	case "length": // OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT=16: the hint is truncated.
		attrs = []attribute.KeyValue{attribute.String("x.parts.0.mime_type", "application/octet-stream"), attribute.String("x.parts.0.data", "AQIDBA=="), attribute.String("x.id.part.type", "input_audio"), attribute.String("x.id.part.content", "AQIDBA=="), attribute.String("app.note", "message 3")}
		attrs = append(attrs, arbitrary(16)...)
		event = attrs
	case "query45", "query50", "query57", "query80", "query34", "query28", "query29", "query30":
		value := "https://example.invalid/?content=AQIDBA%3D%3D&mimeType=audio%2Fwav&model=keep#anchor"
		if os.Args[2] == "query80" {
			value = "https://example.invalid/?data=" + strings.Repeat("AQIDBAUG", 30) + "&mimeType=audio/pcm"
		}
		if os.Args[2] == "query34" || os.Args[2] == "query28" || os.Args[2] == "query29" || os.Args[2] == "query30" {
			value = "https://example.invalid/?v=AAAAIGZ0eXBpc29tMDAwMDAwMDAwMDAwMDAw"
		}
		attrs = []attribute.KeyValue{attribute.String("app.link", value), attribute.String("exception.message", "failed "+value), attribute.String("app.note", "message 3")}
		event = attrs
	case "length8": // OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT=8: a catalog-valid MIME is cut.
		attrs = []attribute.KeyValue{attribute.String("input.id.parts.9.mime_type", " application/octet-stream"), attribute.String("input.id.parts.9.data", short), attribute.String("app.note", "note ok")}
		attrs = append(attrs, arbitrary(8)...)
		event = attrs
	}
	_, target := tracer.Start(context.Background(), "rc2-link-target")
	target.End()
	_, span := tracer.Start(context.Background(), "rc2-zero-code", trace.WithLinks(trace.Link{SpanContext: target.SpanContext(), Attributes: event}))
	span.SetAttributes(attrs...)
	span.AddEvent("rc2-event", trace.WithAttributes(event...))
	span.End()
}
