package auto

import (
	"context"
	"strconv"
	"strings"

	"github.com/future-agi/traceAI/go/auto/internal/wire"
	"go.opentelemetry.io/otel/attribute"
)

const RedactedValue = "__REDACTED__"

// TraceConfig uses the same switch names as the existing Go middleware modules.
// Its values follow the shared rule (see the README), which those modules do not yet.
// Audio transcripts and raw JSON receive additional content redaction so that
// hiding text cannot leak it through a second attribute.
type TraceConfig struct {
	HideLLMInvocationParameters bool
	HideInputs                  bool
	HideOutputs                 bool
	HideInputMessages           bool
	HideOutputMessages          bool
	HideInputImages             bool
	HideInputText               bool
	HideOutputText              bool
	Base64ImageMaxLength        int
}

func traceConfigValues(get func(string, string) string, ignore map[string]bool) TraceConfig {
	max, _ := strconv.Atoi(get("BASE64_IMAGE_MAX_LENGTH", "32000"))
	if max <= 0 {
		max = 32000
	}
	// A switch that a code option replaces is not read, so it cannot print a
	// warning about content that the option then decides.
	hide := func(key string) bool {
		if ignore["*"] || ignore[key] {
			return false
		}
		return hideValue(key, get(key, ""))
	}
	return TraceConfig{Base64ImageMaxLength: max, HideLLMInvocationParameters: hide("HIDE_LLM_INVOCATION_PARAMETERS"), HideInputs: hide("HIDE_INPUTS"), HideOutputs: hide("HIDE_OUTPUTS"), HideInputMessages: hide("HIDE_INPUT_MESSAGES"), HideOutputMessages: hide("HIDE_OUTPUT_MESSAGES"), HideInputImages: hide("HIDE_INPUT_IMAGES"), HideInputText: hide("HIDE_INPUT_TEXT"), HideOutputText: hide("HIDE_OUTPUT_TEXT")}
}
func (c TraceConfig) Mask(kv attribute.KeyValue) (attribute.KeyValue, bool) {
	return c.mask(kv, "")
}
func (c TraceConfig) mask(kv attribute.KeyValue, operation string) (attribute.KeyValue, bool) {
	k := string(kv.Key)
	in := strings.HasPrefix(k, "gen_ai.input.")
	out := strings.HasPrefix(k, "gen_ai.output.")
	input := strings.HasPrefix(k, "input.")
	output := strings.HasPrefix(k, "output.")
	switch {
	case hidesContent(c) && (k == "error.message" || k == "exception.message" || k == "exception.stacktrace"):
		return attribute.String(k, RedactedValue), true
	case (c.HideInputs || c.HideInputText || c.HideInputMessages) && (strings.HasPrefix(k, "gen_ai.system_instructions") || strings.HasPrefix(k, "gen_ai.tool.") && !strings.HasPrefix(k, "gen_ai.tool.call.result")):
		return attribute.String(k, RedactedValue), true
	case (c.HideOutputs || c.HideOutputText || c.HideOutputMessages) && strings.HasPrefix(k, "gen_ai.tool.call.result"):
		return attribute.String(k, RedactedValue), true
	case (c.HideLLMInvocationParameters || c.HideInputs || c.HideInputText || c.HideInputMessages) && attributeFamily(k, "gen_ai.request.parameters"):
		// Native provider extensions (FIM suffix, Bedrock toolConfig, etc.) can
		// contain arbitrary content. Keep typed numeric parameters separately,
		// but never export the opaque blob when input content is hidden.
		return kv, false
	case (c.HideInputs || c.HideInputText || c.HideInputMessages) && input:
		return attribute.String(k, RedactedValue), true
	case (c.HideOutputs || c.HideOutputText || c.HideOutputMessages) && output:
		return attribute.String(k, RedactedValue), true
	case (c.HideInputs || c.HideInputMessages) && in:
		return kv, false
	case (c.HideOutputs || c.HideOutputMessages) && out:
		return kv, false
	case hidesContent(c) && strings.HasPrefix(k, "gen_ai.tool.") && !strings.HasPrefix(k, "gen_ai.tool.call.arguments") && !strings.HasPrefix(k, "gen_ai.tool.definitions") && !strings.HasPrefix(k, "gen_ai.tool.call.result"):
		return attribute.String(k, RedactedValue), true
	case attributeFamily(k, "gen_ai.audio.transcript"):
		hide := c.HideOutputs || c.HideOutputText
		if operation == "text_to_speech" {
			hide = c.HideInputs || c.HideInputText
		} else if operation == "" {
			hide = hide || c.HideInputs || c.HideInputText
		}
		if hide {
			return attribute.String(k, RedactedValue), true
		}
	case c.HideInputImages && wire.ImageAttribute(k):
		return kv, false
	case (c.HideInputText && in || c.HideOutputText && out) && !strings.HasSuffix(k, ".role"):
		return attribute.String(k, RedactedValue), true
	}
	return kv, true
}

// A documented family includes its root and flattened/indexed descendants, but
// not similarly named neighbors (for example, transcript_metadata).
func attributeFamily(key, root string) bool {
	return key == root || strings.HasPrefix(key, root+".")
}

func (c TraceConfig) Filter(kvs []attribute.KeyValue) []attribute.KeyValue {
	out := make([]attribute.KeyValue, 0, len(kvs))
	for _, kv := range kvs {
		if v, ok := c.Mask(kv); ok {
			out = append(out, v)
		}
	}
	return out
}

// Raw media and credential recognition belongs exclusively to wire's S6 catalog
// walker. This optional pass implements only the stronger hide-images policy.
func sanitize(v any, c TraceConfig) any {
	if !c.HideInputImages {
		return v
	}
	return wire.HideImageReferences(v, RedactedValue)
}

// Diagnostics may precede successful initialization, so also mask credentials
// from the proposed configuration even if they have not entered the registry.
func safeString(s string, c Config) string {
	secrets := append([]string{c.APIKey, c.SecretKey}, processSecrets.snapshot().values...)
	return sanitize(wire.PrivacyString(s, secrets), c.TraceConfig).(string)
}

// Capture formatting performs only structural privacy. Value replacement waits
// for S1: replacing a known prefix early could expose a longer secret learned
// by an inner auth transport or while a span waits in the export queue.
func captureString(s string, c Config) string {
	return sanitize(wire.PrivacyString(s, nil), c.TraceConfig).(string)
}
func attributes(values map[string]any, c Config) []attribute.KeyValue {
	kvs := make([]attribute.KeyValue, 0, len(values))
	for k, v := range values {
		switch v := v.(type) {
		case string:
			kvs = append(kvs, attribute.String(k, captureString(v, c)))
		case []string:
			vv := append([]string(nil), v...)
			for i, s := range vv {
				vv[i] = captureString(s, c)
			}
			kvs = append(kvs, attribute.StringSlice(k, vv))
		case int:
			kvs = append(kvs, attribute.Int(k, v))
		case int64:
			kvs = append(kvs, attribute.Int64(k, v))
		case float64:
			kvs = append(kvs, attribute.Float64(k, v))
		case bool:
			kvs = append(kvs, attribute.Bool(k, v))
		}
	}
	operation, _ := values["gen_ai.operation.name"].(string)
	out := kvs[:0]
	for _, kv := range kvs {
		if v, keep := c.TraceConfig.mask(kv, operation); keep {
			out = append(out, v)
		}
	}
	return out
}

type contextKey int

const (
	suppressionKey contextKey = iota
	captureKey
	attributesKey
	sessionKey
)

// SuppressInstrumentation prevents wire capture (including nested transports).
// Go OTel does not define a public cross-instrumentation suppression key.
func SuppressInstrumentation(ctx context.Context) context.Context {
	return context.WithValue(ctx, suppressionKey, true)
}

// ContextWithAttributes carries user.id, metadata, or other context attributes
// to wire children. Keys private to other modules cannot be read. A session.id
// given here is redacted like any credential-named key; use
// ContextWithSessionID to export a session id.
func ContextWithAttributes(ctx context.Context, kvs ...attribute.KeyValue) context.Context {
	old, _ := ctx.Value(attributesKey).([]attribute.KeyValue)
	all := append(append([]attribute.KeyValue(nil), old...), kvs...)
	return context.WithValue(ctx, attributesKey, all)
}

// ContextWithSessionID sets the session id the agent writes as session.id on
// wire spans under ctx. It is exported as its value: unlike a session-like
// name from headers, bodies or other attributes, it is not redacted for its
// name. Known secrets and credential-shaped values inside it are still
// redacted, and a value cut by the provider's value-length limit is scrubbed
// like any other cut value. It replaces a session.id set with
// ContextWithAttributes. A session.id that another span processor sets or
// replaces on the wire span is redacted.
func ContextWithSessionID(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, sessionKey, id)
}
func ContextWithUserID(ctx context.Context, id string) context.Context {
	return ContextWithAttributes(ctx, attribute.String("user.id", id))
}
