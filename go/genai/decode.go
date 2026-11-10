package traceaigenai

import (
	"encoding/json"
	"errors"

	"google.golang.org/genai"
)

// normalizeGenerationResponse mirrors the supported Gemini backend's map ->
// field selection -> public response type pipeline. Raw partial-struct decoding
// differs on duplicate keys, casing, numeric normalization and ignored fields.
// Validate with the installed SDK types, but never export the error or body.
func normalizeGenerationResponse(body []byte, stream bool) ([]byte, error) {
	var raw map[string]any
	// An empty unary body is an empty response in the SDK. Streaming callers
	// reject an empty data block before reaching here.
	if len(body) > 0 {
		if err := json.Unmarshal(body, &raw); err != nil {
			return nil, err
		}
	}
	selected := selectFields(raw, "modelVersion", "promptFeedback", "responseId", "usageMetadata", "modelStatus", "error")
	if stream {
		// Streaming conversion validates this field before replacing its headers.
		selected["sdkHttpResponse"] = raw["sdkHttpResponse"]
	}
	if value := raw["candidates"]; value != nil {
		candidates, ok := value.([]any)
		if !ok {
			return nil, errors.New("invalid candidates")
		}
		out := make([]any, 0, len(candidates))
		for _, value := range candidates {
			candidate, ok := value.(map[string]any)
			if !ok {
				return nil, errors.New("invalid candidate")
			}
			c := selectFields(candidate, "content", "tokenCount", "finishReason", "groundingMetadata", "avgLogprobs", "index", "logprobsResult", "safetyRatings", "urlContextMetadata", "continuationToken")
			if value := candidate["citationMetadata"]; value != nil {
				citation, ok := value.(map[string]any)
				if !ok {
					return nil, errors.New("invalid citation metadata")
				}
				c["citationMetadata"] = map[string]any{"citations": citation["citationSources"]}
			}
			out = append(out, c)
		}
		selected["candidates"] = out
	}
	// Unary sdkHttpResponse comes from actual HTTP headers, not the body.
	// Validate all selected content, including fields we do not export.
	normalized, err := json.Marshal(selected)
	if err != nil {
		return nil, err
	}
	var response genai.GenerateContentResponse
	if err := json.Unmarshal(normalized, &response); err != nil {
		return nil, err
	}
	return normalized, nil
}

func selectFields(raw map[string]any, keys ...string) map[string]any {
	out := make(map[string]any, len(keys))
	for _, key := range keys {
		if value := raw[key]; value != nil {
			out[key] = value
		}
	}
	return out
}
