package traceaigenai_test

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"

	"google.golang.org/genai"
)

func TestToolAndGroundingPrivacy(t *testing.T) {
	for _, stream := range []bool{false, true} {
		for _, tc := range []struct {
			flag                     string
			hideTools, hideGrounding bool
		}{
			{"", false, false},
			{"FI_HIDE_INPUTS", true, true},
			{"FI_HIDE_INPUT_MESSAGES", true, true},
			{"FI_HIDE_INPUT_TEXT", true, true},
			{"FI_HIDE_OUTPUTS", false, true},
			{"FI_HIDE_OUTPUT_MESSAGES", false, true},
			{"FI_HIDE_OUTPUT_TEXT", false, true},
			{"FI_HIDE_LLM_INVOCATION_PARAMETERS", true, false},
			{"FI_HIDE_INPUT_IMAGES", false, false},
			{"FI_HIDE_ERROR_DETAILS", false, false},
		} {
			t.Run(fmt.Sprintf("stream=%t/%s", stream, tc.flag), func(t *testing.T) {
				qClearEnv(t)
				if tc.flag != "" {
					t.Setenv(tc.flag, "true")
				}
				c, _, exp := qSetup(t, func(w http.ResponseWriter, r *http.Request) {
					body, _ := io.ReadAll(r.Body)
					for _, want := range []string{`"googleSearch"`, `"ANY"`, "constraint_only_private", qRequest} {
						if !strings.Contains(string(body), want) {
							t.Errorf("privacy changed wire input: %s", want)
						}
					}
					qReply(groundingBody, stream)(w, r)
				}, true)
				cfg := &genai.GenerateContentConfig{
					Tools: []*genai.Tool{{GoogleSearch: &genai.GoogleSearch{}}},
					ToolConfig: &genai.ToolConfig{FunctionCallingConfig: &genai.FunctionCallingConfig{
						Mode: genai.FunctionCallingConfigModeAny, AllowedFunctionNames: []string{"constraint_only_private"},
					}},
				}
				if err := qGenerate(c, context.Background(), stream, genai.Text(qRequest), cfg); err != nil {
					t.Fatal(err)
				}
				s := qOne(t, exp)
				attrs, all := qAttrs(s), qAll(s)
				for _, key := range []string{toolChoiceKey, builtinToolsKey} {
					if (attrs[key] == "") != tc.hideTools {
						t.Errorf("%s hide=%t got=%q", key, tc.hideTools, attrs[key])
					}
				}
				if tc.hideTools && strings.Contains(all, "constraint_only_private") {
					t.Error("hidden constraint name exported")
				}
				if (attrs[groundingKey] == "") != tc.hideGrounding {
					t.Errorf("grounding hide=%t got=%q", tc.hideGrounding, attrs[groundingKey])
				}
				if tc.hideGrounding {
					for _, secret := range []string{"https://fixture.invalid/", "Fixture source", "fixture research", "CC-BY"} {
						if strings.Contains(all, secret) {
							t.Errorf("hidden evidence exported: %s", secret)
						}
					}
				}
				if attrs["gen_ai.usage.total_tokens"] != "15" {
					t.Error("privacy lost token counts")
				}
			})
		}
	}
}

func TestGroundingStreamAccumulatesAndDeduplicates(t *testing.T) {
	qClearEnv(t)
	second := `{"candidates":[{"index":0,"groundingMetadata":{"groundingChunks":[{"web":{"uri":"https://fixture.invalid/second-source","title":"Second source"}},{"retrievedContext":{"uri":"gs://fixture-bucket/document","title":"Private corpus title","text":"synthetic-private-document","metadata":{"secret":"synthetic-private-metadata"}}}],"webSearchQueries":["follow-up research"]}}]}`
	c, _, exp := qSetup(t, func(w http.ResponseWriter, r *http.Request) {
		for _, body := range []string{groundingBody, groundingBody, second, second, `{"candidates":[{"index":0,"groundingMetadata":{},"citationMetadata":{}}]}`, `{"usageMetadata":{"totalTokenCount":99}}`} {
			qReply(body, true)(w, r)
		}
	}, true)
	if err := qGenerate(c, context.Background(), true, genai.Text(qRequest), nil); err != nil {
		t.Fatal(err)
	}
	s := qOne(t, exp)
	value := qAttrs(s)[groundingKey]
	var evidence struct {
		Candidates []struct {
			Index     int              `json:"candidate_index"`
			Sources   []map[string]any `json:"sources"`
			Queries   []string         `json:"web_search_queries"`
			Citations []map[string]any `json:"citations"`
		} `json:"candidates"`
	}
	if err := json.Unmarshal([]byte(value), &evidence); err != nil {
		t.Fatal(err)
	}
	if len(evidence.Candidates) != 1 {
		t.Fatalf("candidates=%d", len(evidence.Candidates))
	}
	got := evidence.Candidates[0]
	if got.Index != 0 || len(got.Sources) != 3 || len(got.Queries) != 2 || len(got.Citations) != 1 {
		t.Errorf("lost or duplicated evidence: %s", value)
	}
	if got.Citations[0]["startIndex"] != float64(0) || got.Citations[0]["endIndex"] != float64(8) {
		t.Errorf("citation offsets lost: %v", got.Citations)
	}
	for _, want := range []string{"https://fixture.invalid/retrieved-source", "https://fixture.invalid/second-source", "gs://fixture-bucket/document", "retrievedContext", "fixture research", "follow-up research"} {
		if !strings.Contains(value, want) {
			t.Errorf("stream lost %q", want)
		}
	}
	for _, secret := range []string{"synthetic-private-document", "synthetic-private-metadata", "synthetic-private-html", "c3ludGhldGlj"} {
		if strings.Contains(qAll(s), secret) {
			t.Errorf("opaque metadata leaked: %q", secret)
		}
	}
	if qAttrs(s)["gen_ai.usage.total_tokens"] != "99" {
		t.Error("metadata-only chunks lost final usage")
	}
}
