package traceaigenai

import (
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"testing"
	"unicode/utf8"
)

func TestBuiltinToolAllowList(t *testing.T) {
	body := []byte(`{"tools":[{"googleSearch":{"excludeDomains":["synthetic-private-domain"]},"googleSearchRetrieval":{},"codeExecution":{},"urlContext":{},"googleMaps":{},"retrieval":{"secret":"synthetic-private-store"},"fileSearch":{},"computerUse":{},"enterpriseWebSearch":{},"unknownTool":{"secret":"synthetic-private-unknown"}},{"googleSearch":{}}],"toolConfig":{"retrievalConfig":{"secret":"synthetic-private-location"},"functionCallingConfig":{"mode":"ANY","allowedFunctionNames":["lookup","lookup"],"unknown":"synthetic-private-config"}}}`)
	attrs := parseGenerateRequest(body, TraceConfig{}).attrs
	var tools []string
	var choice string
	for _, kv := range attrs {
		if kv.Key == GenAIRequestBuiltinTools {
			tools = kv.Value.AsStringSlice()
		}
		if kv.Key == GenAIRequestToolChoice {
			choice = kv.Value.AsString()
		}
		if strings.Contains(kv.Value.Emit(), "synthetic-private") {
			t.Errorf("opaque configuration exported: %s", kv.Key)
		}
	}
	want := []string{"googleSearch", "googleSearchRetrieval", "codeExecution", "urlContext", "googleMaps", "retrieval", "fileSearch", "computerUse", "enterpriseWebSearch"}
	if !slices.Equal(tools, want) {
		t.Errorf("tools=%v want=%v", tools, want)
	}
	if choice != `{"mode":"any","allowed_function_names":["lookup"]}` {
		t.Errorf("choice=%q", choice)
	}
}

func TestToolMetadataAbsentAndUnknown(t *testing.T) {
	for _, body := range []string{
		`{}`, `{"tools":[{}],"toolConfig":{}}`,
		`{"tools":[{"googleSearch":null,"unknownTool":{}}],"toolConfig":{"functionCallingConfig":{"mode":"synthetic-private-mode"}}}`,
		`{"toolConfig":{"functionCallingConfig":null}}`,
	} {
		if attrs := parseGenerateRequest([]byte(body), TraceConfig{}).attrs; len(attrs) != 0 {
			t.Errorf("invented metadata for %s: %v", body, attrs)
		}
	}
	attrs := parseGenerateRequest([]byte(`{"toolConfig":{"functionCallingConfig":{"mode":"MODE_UNSPECIFIED"}}}`), TraceConfig{}).attrs
	if len(attrs) != 1 || attrs[0].Value.AsString() != `{"mode":"unspecified"}` {
		t.Errorf("explicit unspecified mode lost: %v", attrs)
	}
}

func TestToolConstraintBounds(t *testing.T) {
	var names []string
	for i := 0; i < 200; i++ {
		names = append(names, fmt.Sprintf("tool_%d_%s", i, strings.Repeat("界", 500)))
	}
	body, _ := json.Marshal(map[string]any{"toolConfig": map[string]any{"functionCallingConfig": map[string]any{"mode": "ANY", "allowedFunctionNames": names}}})
	attrs := parseGenerateRequest(body, TraceConfig{}).attrs
	if len(attrs) != 1 {
		t.Fatalf("constraints consumed %d attribute slots", len(attrs))
	}
	var got struct {
		Names     []string `json:"allowed_function_names"`
		Truncated bool     `json:"truncated"`
	}
	if err := json.Unmarshal([]byte(attrs[0].Value.AsString()), &got); err != nil {
		t.Fatal(err)
	}
	if len(got.Names) != maxAllowedFunctionNames || !got.Truncated {
		t.Errorf("unbounded constraints: %+v", got)
	}
	for _, name := range got.Names {
		if len(name) > maxMetadataStringBytes || !utf8.ValidString(name) || strings.ContainsRune(name, utf8.RuneError) {
			t.Error("string limit split UTF-8 or was exceeded")
		}
	}
}

func TestGroundingBoundsAcrossChunks(t *testing.T) {
	a := newAccumulator()
	a.stream = true
	for i := 0; i < 200; i++ {
		for candidate := 0; candidate < 6; candidate++ {
			ref := map[string]any{"uri": fmt.Sprintf("https://fixture.invalid/source-%d", i), "title": strings.Repeat("界", 500), "text": "synthetic-private-document"}
			body, _ := json.Marshal(map[string]any{"candidates": []any{map[string]any{
				"index": candidate, "groundingMetadata": map[string]any{
					"groundingChunks":  []any{map[string]any{"web": ref}},
					"webSearchQueries": []string{fmt.Sprintf("query-%d-%s", i, strings.Repeat("界", 500))},
				},
				"citationMetadata": map[string]any{"citationSources": []any{map[string]any{"uri": ref["uri"], "license": strings.Repeat("界", 500), "startIndex": 0, "endIndex": 5}}},
			}}})
			a.addResponse(body)
		}
	}
	if a.decodeFailed {
		t.Fatal("SDK rejected bound fixture")
	}
	if len(a.grounding.Candidates) != maxGroundingCandidates || !a.grounding.Truncated {
		t.Fatalf("candidate bound failed: %d", len(a.grounding.Candidates))
	}
	for _, c := range a.grounding.Candidates {
		if len(c.Sources) != maxGroundingSources || len(c.Queries) != maxGroundingQueries || len(c.Citations) != maxGroundingCitations || !c.Truncated {
			t.Errorf("unbounded evidence for candidate %d", c.Index)
		}
		for _, s := range c.Sources {
			if len(s.Title) > maxMetadataStringBytes || !utf8.ValidString(s.Title) || strings.ContainsRune(s.Title, utf8.RuneError) {
				t.Error("source string bound corrupted UTF-8")
			}
		}
	}
	value := a.grounding.json()
	if !json.Valid([]byte(value)) || len(value) > 200_000 || strings.Contains(value, "synthetic-private-document") || strings.Contains(value, "source-199") {
		t.Error("grounding is unbounded or copied opaque data")
	}
}

func TestGroundingNilUnknownAndSparseCandidates(t *testing.T) {
	a := newAccumulator()
	a.addResponse([]byte(`{"candidates":[{"index":7,"groundingMetadata":{"groundingChunks":[null,{}, {"web":null},{"web":{"uri":"https://fixture.invalid/seven"}}],"webSearchQueries":[""]},"citationMetadata":{"citationSources":[null,{}, {"uri":"https://fixture.invalid/citation","startIndex":0}]}},{"index":2,"groundingMetadata":{"groundingChunks":[{"web":{"uri":"https://fixture.invalid/two"}}]}},{"index":3,"groundingMetadata":{"searchEntryPoint":{"renderedContent":"synthetic-private-html"}}}]}`))
	if a.decodeFailed {
		t.Fatal("sparse fixture rejected by SDK")
	}
	if len(a.grounding.Candidates) != 2 {
		t.Fatalf("candidates=%d", len(a.grounding.Candidates))
	}
	value := a.grounding.json()
	if a.grounding.Candidates[0].Index != 2 || a.grounding.Candidates[1].Index != 7 || strings.Contains(value, "synthetic-private-html") {
		t.Errorf("sparse evidence invalid: %s", value)
	}
}
