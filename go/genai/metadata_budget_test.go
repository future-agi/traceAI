package traceaigenai_test

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	traceaigenai "github.com/future-agi/traceAI/go/genai"
	"google.golang.org/genai"
)

func TestMetadataFitsDefaultOTelBudget(t *testing.T) {
	for _, stream := range []bool{false, true} {
		t.Run(fmt.Sprintf("stream=%t", stream), func(t *testing.T) {
			qClearEnv(t)
			contents := make([]*genai.Content, 40)
			for i := range contents {
				contents[i] = genai.NewContentFromText(fmt.Sprintf("turn-%d", i), genai.RoleUser)
			}
			cfg := &genai.GenerateContentConfig{
				Temperature: genai.Ptr(float32(0.5)), TopP: genai.Ptr(float32(0.9)), TopK: genai.Ptr(float32(40)),
				MaxOutputTokens: 100, PresencePenalty: genai.Ptr(float32(0.2)), FrequencyPenalty: genai.Ptr(float32(0.3)), Seed: genai.Ptr(int32(42)), StopSequences: []string{"STOP"},
				Tools:      []*genai.Tool{{GoogleSearch: &genai.GoogleSearch{}, URLContext: &genai.URLContext{}}},
				ToolConfig: &genai.ToolConfig{FunctionCallingConfig: &genai.FunctionCallingConfig{Mode: genai.FunctionCallingConfigModeAny}},
			}
			for i := 0; i < 150; i++ {
				name := fmt.Sprintf("tool_%d", i)
				cfg.Tools[0].FunctionDeclarations = append(cfg.Tools[0].FunctionDeclarations, &genai.FunctionDeclaration{Name: name})
				cfg.ToolConfig.FunctionCallingConfig.AllowedFunctionNames = append(cfg.ToolConfig.FunctionCallingConfig.AllowedFunctionNames, name)
			}
			var chunks, citations, urls []any
			var queries []string
			for i := 0; i < 150; i++ {
				uri := fmt.Sprintf("https://fixture.invalid/source-%d", i)
				chunks = append(chunks, map[string]any{"web": map[string]any{"uri": uri, "title": fmt.Sprintf("Source %d", i)}})
				citations = append(citations, map[string]any{"uri": uri, "startIndex": 0, "endIndex": 5})
				queries = append(queries, fmt.Sprintf("query-%d", i))
				urls = append(urls, map[string]any{"retrievedUrl": uri, "urlRetrievalStatus": "URL_RETRIEVAL_STATUS_SUCCESS"})
			}
			var candidates []any
			for i := 0; i < 20; i++ {
				candidates = append(candidates, map[string]any{
					"index": i, "finishReason": "STOP", "content": map[string]any{"role": "model", "parts": []any{map[string]any{"text": fmt.Sprintf("answer-%d", i)}}},
					"groundingMetadata":  map[string]any{"groundingChunks": chunks, "webSearchQueries": queries},
					"citationMetadata":   map[string]any{"citationSources": citations},
					"urlContextMetadata": map[string]any{"urlMetadata": urls},
				})
			}
			body, _ := json.Marshal(map[string]any{
				"candidates": candidates, "modelVersion": "fixture-version", "responseId": "fixture-response",
				"usageMetadata": map[string]any{"promptTokenCount": 10, "toolUsePromptTokenCount": 2, "candidatesTokenCount": 5, "thoughtsTokenCount": 3, "totalTokenCount": 20, "cachedContentTokenCount": 4,
					"promptTokensDetails": []any{map[string]any{"modality": "TEXT", "tokenCount": 10}}},
			})
			c, _, exp := qSetup(t, qReply(string(body), stream), true)
			ctx := traceaigenai.ContextWithUserID(traceaigenai.ContextWithSessionID(context.Background(), "fixture-session"), "fixture-user")
			if err := qGenerate(c, ctx, stream, contents, cfg); err != nil {
				t.Fatal(err)
			}
			s := qOne(t, exp)
			attrs := qAttrs(s)
			if s.DroppedAttributes != 0 || len(s.Attributes) > 128 {
				t.Errorf("dropped=%d attribute count=%d", s.DroppedAttributes, len(s.Attributes))
			}
			for key, want := range map[string]string{
				"gen_ai.usage.input_tokens": "12", "gen_ai.usage.output_tokens": "8", "gen_ai.usage.total_tokens": "20",
				"gen_ai.response.model": "fixture-version", "gen_ai.response.id": "fixture-response", "session.id": "fixture-session", "user.id": "fixture-user", "http.response.status_code": "200",
			} {
				if attrs[key] != want {
					t.Errorf("%s=%q want=%q", key, attrs[key], want)
				}
			}
			if !strings.Contains(attrs["input.value"], "turn-39") || !strings.Contains(attrs["output.value"], "answer-19") {
				t.Error("preview budget truncated aggregate conversation")
			}
			if !strings.Contains(attrs[toolChoiceKey], `"truncated":true`) || !strings.Contains(attrs[builtinToolsKey], "googleSearch") {
				t.Error("request metadata missing")
			}
			var evidence struct {
				Candidates []struct {
					Sources   []any    `json:"sources"`
					Queries   []string `json:"web_search_queries"`
					Citations []any    `json:"citations"`
					URLs      []struct {
						URL    string `json:"retrieved_url"`
						Status string `json:"url_retrieval_status"`
					} `json:"url_retrievals"`
					Truncated bool `json:"truncated"`
				} `json:"candidates"`
				Truncated bool `json:"truncated"`
			}
			if err := json.Unmarshal([]byte(attrs[groundingKey]), &evidence); err != nil {
				t.Fatal(err)
			}
			if len(evidence.Candidates) != 4 || !evidence.Truncated {
				t.Errorf("candidate evidence not bounded: %d", len(evidence.Candidates))
			}
			for _, candidate := range evidence.Candidates {
				if len(candidate.Sources) != 16 || len(candidate.Queries) != 8 || len(candidate.Citations) != 16 || len(candidate.URLs) != 16 || !candidate.Truncated {
					t.Error("large source list not bounded")
				}
				for i, result := range candidate.URLs {
					if result.URL != fmt.Sprintf("https://fixture.invalid/source-%d", i) || result.Status != "URL_RETRIEVAL_STATUS_SUCCESS" {
						t.Errorf("URL retrieval result lost under default budget: %+v", result)
					}
				}
			}
		})
	}
}
