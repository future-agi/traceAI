package traceaigenai

import (
	"encoding/json"
	"os"
	"strconv"
	"strings"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
)

// Environment variables read by [TraceConfigFromEnv]. They are the traceAI
// TraceConfig switches, with the same names and defaults as the Python and
// TypeScript packages: everything is captured unless a switch is "true".
const (
	EnvHideLLMInvocationParameters = "FI_HIDE_LLM_INVOCATION_PARAMETERS"
	EnvHideInputs                  = "FI_HIDE_INPUTS"
	EnvHideOutputs                 = "FI_HIDE_OUTPUTS"
	EnvHideInputMessages           = "FI_HIDE_INPUT_MESSAGES"
	EnvHideOutputMessages          = "FI_HIDE_OUTPUT_MESSAGES"
	EnvHideInputImages             = "FI_HIDE_INPUT_IMAGES"
	EnvHideInputText               = "FI_HIDE_INPUT_TEXT"
	EnvHideOutputText              = "FI_HIDE_OUTPUT_TEXT"
	EnvHideErrorDetails            = "FI_HIDE_ERROR_DETAILS"
	EnvBase64ImageMaxLength        = "FI_BASE64_IMAGE_MAX_LENGTH"
)

const (
	// RedactedValue replaces input.value / output.value (and message text
	// under the *_TEXT switches) when they are hidden, as in Python traceAI.
	RedactedValue = "__REDACTED__"
	// DefaultBase64ImageMaxLength caps data: image URLs kept in input messages.
	DefaultBase64ImageMaxLength = 32_000
)

// TraceConfig decides which content attributes leave the process. The zero
// value captures everything, like DEFAULT_HIDE_* = False in Python traceAI.
type TraceConfig struct {
	// HideLLMInvocationParameters drops gen_ai.request.parameters, tool_choice
	// and builtin_tools, and reduces tool declarations to function names only.
	// The same tool policy applies under HideInputs, HideInputMessages and HideInputText.
	HideLLMInvocationParameters bool
	// HideInputs redacts input.value and drops input messages, system
	// instructions and tool-call arguments.
	HideInputs bool
	// HideOutputs redacts output.value and drops output messages and tool-call
	// results.
	HideOutputs bool
	// HideInputMessages drops gen_ai.input.messages.*, tool selection and
	// grounding metadata, but preserves input.value.
	HideInputMessages bool
	// HideOutputMessages drops gen_ai.output.messages.* and grounding metadata,
	// but preserves output.value.
	HideOutputMessages bool
	// HideInputImages drops images from input messages and input.value.
	HideInputImages bool
	// HideInputText redacts text in input messages and input.value, and omits
	// tool selection and grounding metadata.
	HideInputText bool
	// HideOutputText redacts text in output messages and output.value, and
	// omits grounding metadata.
	HideOutputText bool
	// HideErrorDetails drops API error messages from attributes and status
	// descriptions. Nil derives the setting from HideInputs, HideOutputs,
	// HideInputMessages, HideOutputMessages, HideInputText and HideOutputText.
	// An explicit true or false overrides that derivation, as in core traceAI.
	HideErrorDetails *bool
	// Base64ImageMaxLength redacts data: image URLs longer than this. Zero
	// means DefaultBase64ImageMaxLength.
	Base64ImageMaxLength int
}

// TraceConfigFromEnv reads the FI_HIDE_* variables. A value is true only when
// it is "true" (any case) and false otherwise, matching Python traceAI, which
// falls back to the default for anything it cannot parse.
func TraceConfigFromEnv() TraceConfig {
	c := TraceConfig{
		HideLLMInvocationParameters: envBool(EnvHideLLMInvocationParameters),
		HideInputs:                  envBool(EnvHideInputs),
		HideOutputs:                 envBool(EnvHideOutputs),
		HideInputMessages:           envBool(EnvHideInputMessages),
		HideOutputMessages:          envBool(EnvHideOutputMessages),
		HideInputImages:             envBool(EnvHideInputImages),
		HideInputText:               envBool(EnvHideInputText),
		HideOutputText:              envBool(EnvHideOutputText),
	}
	switch value := strings.ToLower(strings.TrimSpace(os.Getenv(EnvHideErrorDetails))); value {
	case "true", "false":
		hide := value == "true"
		c.HideErrorDetails = &hide
	}
	if v, err := strconv.Atoi(strings.TrimSpace(os.Getenv(EnvBase64ImageMaxLength))); err == nil && v > 0 {
		c.Base64ImageMaxLength = v
	}
	return c
}

func envBool(name string) bool {
	return strings.EqualFold(strings.TrimSpace(os.Getenv(name)), "true")
}

// errorMessageAllowed reports whether the API's error message may be written
// into the span status, which TraceConfig.Mask does not see.
func (c TraceConfig) errorMessageAllowed() bool {
	if c.HideErrorDetails != nil {
		return !*c.HideErrorDetails
	}
	return !(c.HideInputs || c.HideOutputs || c.HideInputMessages || c.HideOutputMessages || c.HideInputText || c.HideOutputText)
}

func (c TraceConfig) imageMax() int {
	if c.Base64ImageMaxLength > 0 {
		return c.Base64ImageMaxLength
	}
	return DefaultBase64ImageMaxLength
}

// Mask applies the config to one attribute. It returns the value to write and
// false when the attribute must be dropped. The rules and their order follow
// fi_instrumentation.TraceConfig.mask in Python traceAI. System instructions
// and tool-call arguments go with the inputs, tool-call results with the
// outputs. Tool declarations follow input and invocation hiding, retaining only
// function names (the Go integration's tool identity contract), not descriptions
// or schemas. Grounding URLs, titles, queries and citations can quote either the
// prompt or answer, so the entire grounding attribute is omitted under any
// input/output content, message or text hiding switch. This is intentionally
// more conservative than message-only filtering of input.value / output.value.
// The API's error message follows HideErrorDetails because an API error can
// quote the request or response.
func (c TraceConfig) Mask(kv attribute.KeyValue) (attribute.KeyValue, bool) {
	key := string(kv.Key)
	in := strings.Contains(key, GenAIInputMessages)
	out := strings.Contains(key, GenAIOutputMessages)
	redacted := attribute.String(key, RedactedValue)
	switch {
	case (c.HideLLMInvocationParameters || c.HideInputs || c.HideInputMessages || c.HideInputText) &&
		(key == GenAIToolDefinitions || strings.HasPrefix(key, GenAIToolDefinitions+".")):
		return toolDefinitionIdentity(kv)
	case (c.HideLLMInvocationParameters || c.HideInputs || c.HideInputMessages || c.HideInputText) &&
		(key == GenAIRequestToolChoice || key == GenAIRequestBuiltinTools):
		return kv, false
	case key == GenAIResponseGrounding &&
		(c.HideInputs || c.HideInputMessages || c.HideInputText || c.HideOutputs || c.HideOutputMessages || c.HideOutputText):
		return kv, false
	case c.HideLLMInvocationParameters && key == GenAIRequestParameters:
		return kv, false
	case c.HideInputs && key == InputValue:
		return redacted, true
	case c.HideInputs && key == InputMimeType:
		return kv, false
	case c.HideOutputs && key == OutputValue:
		return redacted, true
	case c.HideOutputs && key == OutputMimeType:
		return kv, false
	case (c.HideInputs || c.HideInputMessages) && in:
		return kv, false
	case (c.HideOutputs || c.HideOutputMessages) && out:
		return kv, false
	case c.HideInputs && (key == GenAISystemInstructions || key == GenAIToolCallArguments):
		return kv, false
	case c.HideOutputs && key == GenAIToolCallResult:
		return kv, false
	case !c.errorMessageAllowed() && key == ErrorMessage:
		return kv, false
	case c.HideInputText && in && strings.Contains(key, MessageContent) && !strings.Contains(key, MessageContents):
		return redacted, true
	case c.HideOutputText && out && strings.Contains(key, MessageContent) && !strings.Contains(key, MessageContents):
		return redacted, true
	case c.HideInputText && in && strings.Contains(key, MessageContentText):
		return redacted, true
	case c.HideOutputText && out && strings.Contains(key, MessageContentText):
		return redacted, true
	case c.HideInputImages && in && strings.Contains(key, MessageContentImage):
		return kv, false
	case in && strings.Contains(key, MessageContentImage) && kv.Value.Type() == attribute.STRING &&
		isBase64ImageURL(kv.Value.AsString()) && len(kv.Value.AsString()) > c.imageMax():
		return redacted, true
	}
	return kv, true
}

// toolDefinitionIdentity retains the declared function name, as for tool-call
// identity elsewhere, but no customer-authored description or schema. Schema
// keys, enum values, defaults and nested metadata can all contain private text;
// removing only description fields is insufficient. Invalid values fail closed.
func toolDefinitionIdentity(kv attribute.KeyValue) (attribute.KeyValue, bool) {
	if kv.Value.Type() != attribute.STRING || !strings.HasSuffix(string(kv.Key), "."+ToolJSONSchema) {
		return kv, false
	}
	var identity struct {
		Name string `json:"name"`
	}
	if err := json.Unmarshal([]byte(kv.Value.AsString()), &identity); err != nil || identity.Name == "" {
		return kv, false
	}
	b, err := json.Marshal(identity)
	if err != nil {
		return kv, false
	}
	return attribute.String(string(kv.Key), string(b)), true
}

// Filter applies [TraceConfig.Mask] to every attribute and returns the ones to
// write.
func (c TraceConfig) Filter(kvs []attribute.KeyValue) []attribute.KeyValue {
	out := make([]attribute.KeyValue, 0, len(kvs))
	for _, kv := range kvs {
		if m, keep := c.Mask(kv); keep {
			out = append(out, m)
		}
	}
	return out
}

// SetAttributes writes the attributes that survive the config onto span. Use
// it for spans your own code starts (an agent step, a tool call) so they obey
// the same FI_HIDE_* switches as the instrumentor's spans.
func (c TraceConfig) SetAttributes(span trace.Span, kvs ...attribute.KeyValue) {
	if span == nil {
		return
	}
	span.SetAttributes(c.Filter(kvs)...)
}

func isBase64ImageURL(s string) bool {
	return strings.HasPrefix(s, "data:image/") && strings.Contains(s, "base64")
}
