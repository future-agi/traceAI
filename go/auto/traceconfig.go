package auto

import (
	"context"
	"strconv"
	"strings"

	"github.com/future-agi/traceAI/go/auto/internal/wire"
	"go.opentelemetry.io/otel/attribute"
)

const RedactedValue = "__REDACTED__"

// TraceConfig uses the same switches as the existing Go middleware modules.
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

func traceConfigValues(get func(string, string) string) TraceConfig {
	max, _ := strconv.Atoi(get("BASE64_IMAGE_MAX_LENGTH", "32000"))
	if max <= 0 {
		max = 32000
	}
	return TraceConfig{Base64ImageMaxLength: max, HideLLMInvocationParameters: boolValue(get("HIDE_LLM_INVOCATION_PARAMETERS", "false")), HideInputs: boolValue(get("HIDE_INPUTS", "false")), HideOutputs: boolValue(get("HIDE_OUTPUTS", "false")), HideInputMessages: boolValue(get("HIDE_INPUT_MESSAGES", "false")), HideOutputMessages: boolValue(get("HIDE_OUTPUT_MESSAGES", "false")), HideInputImages: boolValue(get("HIDE_INPUT_IMAGES", "false")), HideInputText: boolValue(get("HIDE_INPUT_TEXT", "false")), HideOutputText: boolValue(get("HIDE_OUTPUT_TEXT", "false"))}
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
)

// SuppressInstrumentation prevents wire capture (including nested transports).
// Go OTel does not define a public cross-instrumentation suppression key.
func SuppressInstrumentation(ctx context.Context) context.Context {
	return context.WithValue(ctx, suppressionKey, true)
}

// ContextWithAttributes carries session.id, user.id, metadata, or other context
// attributes to wire children. Keys private to other modules cannot be read.
func ContextWithAttributes(ctx context.Context, kvs ...attribute.KeyValue) context.Context {
	old, _ := ctx.Value(attributesKey).([]attribute.KeyValue)
	all := append(append([]attribute.KeyValue(nil), old...), kvs...)
	return context.WithValue(ctx, attributesKey, all)
}
func ContextWithSessionID(ctx context.Context, id string) context.Context {
	return ContextWithAttributes(ctx, attribute.String("session.id", id))
}
func ContextWithUserID(ctx context.Context, id string) context.Context {
	return ContextWithAttributes(ctx, attribute.String("user.id", id))
}
