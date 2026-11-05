package traceaigenai

import (
	"encoding/json"
	"slices"
	"strings"
	"unicode/utf8"

	"go.opentelemetry.io/otel/attribute"
)

const (
	maxAllowedFunctionNames = 32
	maxMetadataStringBytes  = 512
)

type toolConfig struct {
	FunctionCallingConfig *struct {
		Mode                 string   `json:"mode"`
		AllowedFunctionNames []string `json:"allowedFunctionNames"`
	} `json:"functionCallingConfig"`
}

// toolConfigAttributes uses two slots regardless of the number of tools. It
// never copies search filters, retrieval stores, locations, computer-use
// configuration or unknown provider options. Function names are bounded input
// data; both attributes follow invocation-parameter and input privacy switches.
func toolConfigAttributes(r generateRequest) []attribute.KeyValue {
	var attrs []attribute.KeyValue
	var builtins []string
	for _, t := range r.Tools {
		for _, builtin := range []struct {
			name    string
			present bool
		}{
			{"googleSearch", t.GoogleSearch != nil},
			{"googleSearchRetrieval", t.GoogleSearchRetrieval != nil},
			{"codeExecution", t.CodeExecution != nil},
			{"urlContext", t.URLContext != nil},
			{"googleMaps", t.GoogleMaps != nil},
			{"retrieval", t.Retrieval != nil},
			{"fileSearch", t.FileSearch != nil},
			{"computerUse", t.ComputerUse != nil},
			{"enterpriseWebSearch", t.EnterpriseWebSearch != nil},
		} {
			if builtin.present && !slices.Contains(builtins, builtin.name) {
				builtins = append(builtins, builtin.name)
			}
		}
	}
	if len(builtins) > 0 {
		attrs = append(attrs, attribute.StringSlice(GenAIRequestBuiltinTools, builtins))
	}
	if r.ToolConfig == nil || r.ToolConfig.FunctionCallingConfig == nil {
		return attrs
	}
	f := r.ToolConfig.FunctionCallingConfig
	choice := struct {
		Mode      string   `json:"mode,omitempty"`
		Names     []string `json:"allowed_function_names,omitempty"`
		Truncated bool     `json:"truncated,omitempty"`
	}{}
	switch f.Mode {
	case "AUTO", "ANY", "NONE", "VALIDATED":
		choice.Mode = strings.ToLower(f.Mode)
	case "MODE_UNSPECIFIED":
		choice.Mode = "unspecified"
	}
	for _, name := range f.AllowedFunctionNames {
		if name == "" {
			continue
		}
		name = boundedMetadataString(name, &choice.Truncated)
		if slices.Contains(choice.Names, name) {
			continue
		}
		if len(choice.Names) == maxAllowedFunctionNames {
			choice.Truncated = true
			break
		}
		choice.Names = append(choice.Names, name)
	}
	if choice.Mode != "" || len(choice.Names) > 0 {
		b, _ := json.Marshal(choice)
		attrs = append(attrs, attribute.String(GenAIRequestToolChoice, string(b)))
	}
	return attrs
}

// Preserve UTF-8 boundaries and signal truncation instead of silently exporting
// a seemingly complete set of constraints or retrieval evidence.
func boundedMetadataString(s string, truncated *bool) string {
	if len(s) <= maxMetadataStringBytes {
		return s
	}
	*truncated = true
	n := maxMetadataStringBytes
	for n > 0 && !utf8.RuneStart(s[n]) {
		n--
	}
	// A substring alone would retain the entire provider string in the stream
	// accumulator even though only its prefix is exported.
	return strings.Clone(s[:n])
}
