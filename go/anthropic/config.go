package traceaianthropic

import (
	"log"
	"os"
	"strconv"
	"strings"
)

// Environment variables. The names, defaults and the redaction sentinel are the
// ones in traceAI's Python TraceConfig
// (python/fi_instrumentation/instrumentation/config.py), so one set of
// variables controls every traceAI instrumentor in a deployment.
const (
	EnvHideLLMInvocationParameters = "FI_HIDE_LLM_INVOCATION_PARAMETERS"
	EnvHideInputs                  = "FI_HIDE_INPUTS"
	EnvHideOutputs                 = "FI_HIDE_OUTPUTS"
	EnvHideInputMessages           = "FI_HIDE_INPUT_MESSAGES"
	EnvHideOutputMessages          = "FI_HIDE_OUTPUT_MESSAGES"
	EnvHideInputImages             = "FI_HIDE_INPUT_IMAGES"
	EnvHideInputText               = "FI_HIDE_INPUT_TEXT"
	EnvHideOutputText              = "FI_HIDE_OUTPUT_TEXT"
	EnvBase64ImageMaxLength        = "FI_BASE64_IMAGE_MAX_LENGTH"
)

// DefaultBase64ImageMaxLength is the default of [TraceConfig.Base64ImageMaxLength].
const DefaultBase64ImageMaxLength = 32_000

// TraceConfig is how much of a request and a response the spans carry. The zero
// value captures everything, which is the traceAI default; each Hide field turns
// one kind of content off. Use [TraceConfigFromEnv] to read the FI_HIDE_*
// variables, then change fields in code where you want to override them.
type TraceConfig struct {
	// HideLLMInvocationParameters leaves out gen_ai.request.parameters.
	HideLLMInvocationParameters bool
	// HideInputs replaces input.value with __REDACTED__ and leaves out
	// input.mime_type, the input messages, the system instructions, the tool
	// definitions and the stop sequences.
	HideInputs bool
	// HideOutputs replaces output.value with __REDACTED__ and leaves out
	// output.mime_type and the output messages.
	HideOutputs bool
	// HideInputMessages leaves out gen_ai.input.messages and
	// gen_ai.system_instructions.
	HideInputMessages bool
	// HideOutputMessages leaves out gen_ai.output.messages.
	HideOutputMessages bool
	// HideInputImages leaves image parts out of gen_ai.input.messages.
	HideInputImages bool
	// HideInputText replaces the text of every part of gen_ai.input.messages and
	// gen_ai.system_instructions with __REDACTED__.
	HideInputText bool
	// HideOutputText replaces the text of every part of gen_ai.output.messages
	// with __REDACTED__.
	HideOutputText bool
	// Base64ImageMaxLength is the longest base64 image that the spans keep;
	// a longer one is replaced with __REDACTED__. Zero or less means
	// DefaultBase64ImageMaxLength.
	Base64ImageMaxLength int
}

// TraceConfigFromEnv returns the TraceConfig that the FI_HIDE_* and
// FI_BASE64_IMAGE_MAX_LENGTH environment variables describe. A variable that is
// unset keeps the default. A boolean variable is "true" or "false" in any case;
// any other value logs a warning and keeps the default, as the Python
// TraceConfig does.
func TraceConfigFromEnv() TraceConfig {
	return TraceConfig{
		HideLLMInvocationParameters: envBool(EnvHideLLMInvocationParameters),
		HideInputs:                  envBool(EnvHideInputs),
		HideOutputs:                 envBool(EnvHideOutputs),
		HideInputMessages:           envBool(EnvHideInputMessages),
		HideOutputMessages:          envBool(EnvHideOutputMessages),
		HideInputImages:             envBool(EnvHideInputImages),
		HideInputText:               envBool(EnvHideInputText),
		HideOutputText:              envBool(EnvHideOutputText),
		Base64ImageMaxLength:        envInt(EnvBase64ImageMaxLength, DefaultBase64ImageMaxLength),
	}
}

func envBool(name string) bool {
	v, ok := os.LookupEnv(name)
	if !ok {
		return false
	}
	switch strings.ToLower(strings.TrimSpace(v)) {
	case "true":
		return true
	case "false":
		return false
	}
	log.Printf("traceaianthropic: could not parse %q for %s; using the default, false", v, name)
	return false
}

func envInt(name string, def int) int {
	v, ok := os.LookupEnv(name)
	if !ok {
		return def
	}
	n, err := strconv.Atoi(strings.TrimSpace(v))
	if err != nil {
		log.Printf("traceaianthropic: could not parse %q for %s; using the default, %d", v, name, def)
		return def
	}
	return n
}

func (c TraceConfig) imageMax() int {
	if c.Base64ImageMaxLength <= 0 {
		return DefaultBase64ImageMaxLength
	}
	return c.Base64ImageMaxLength
}

func (c TraceConfig) inputMessagesHidden() bool  { return c.HideInputs || c.HideInputMessages }
func (c TraceConfig) outputMessagesHidden() bool { return c.HideOutputs || c.HideOutputMessages }
