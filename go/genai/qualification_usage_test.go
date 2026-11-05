package traceaigenai_test

import (
	"context"
	"fmt"
	"strings"
	"testing"

	"google.golang.org/genai"
)

// Google's totalTokenCount definition is prompt + tool-use prompt + thoughts +
// response candidates. Tool-use prompts are input, not completion tokens.
func TestQualificationToolUsePromptAccounting(t *testing.T) {
	const response = `{"candidates":[{"index":0,"content":{"role":"model","parts":[{"text":"answer"}]},"finishReason":"STOP"}],"modelVersion":"gemini-test","usageMetadata":{"promptTokenCount":10,"toolUsePromptTokenCount":7,"candidatesTokenCount":3,"thoughtsTokenCount":2,"totalTokenCount":22,"cachedContentTokenCount":4,"promptTokensDetails":[{"modality":"AUDIO","tokenCount":6},{"modality":"TEXT","tokenCount":4}],"toolUsePromptTokensDetails":[{"modality":"TEXT","tokenCount":7}]}}`
	for _, stream := range []bool{false, true} {
		t.Run(fmt.Sprintf("stream=%t", stream), func(t *testing.T) {
			qClearEnv(t)
			c, _, exp := qSetup(t, qReply(response, stream), true)
			var res *genai.GenerateContentResponse
			if stream {
				for v, err := range c.Models.GenerateContentStream(context.Background(), "gemini-test", genai.Text(qRequest), nil) {
					if err != nil {
						t.Fatal(err)
					}
					res = v
				}
			} else {
				var err error
				res, err = c.Models.GenerateContent(context.Background(), "gemini-test", genai.Text(qRequest), nil)
				if err != nil {
					t.Fatal(err)
				}
			}
			if res == nil || res.UsageMetadata == nil || res.UsageMetadata.ToolUsePromptTokenCount != 7 {
				t.Fatalf("SDK oracle did not preserve tool-use tokens: %+v", res)
			}
			a := qAttrs(qOne(t, exp))
			for _, value := range []string{`"toolUsePromptTokensDetails":[{"modality":"TEXT","tokenCount":7}]`, `"modality":"AUDIO","tokenCount":6`} {
				if !strings.Contains(a["gen_ai.usage.details"], value) {
					t.Errorf("missing modality counts: %s", a["gen_ai.usage.details"])
				}
			}
			for key, want := range map[string]string{"gen_ai.usage.total_tokens": "22", "gen_ai.usage.output_tokens": "5", "gen_ai.usage.cache_read_tokens": "4", "gen_ai.usage.output_tokens.reasoning": "2"} {
				if a[key] != want {
					t.Errorf("%s=%q want=%q", key, a[key], want)
				}
			}
			if a["gen_ai.usage.input_tokens"] != "17" {
				t.Errorf("tool-use prompt tokens lost: input=%q want=17 (prompt10 + tool7), output=%q total=%q; real SDK toolUsePromptTokenCount=%d", a["gen_ai.usage.input_tokens"], a["gen_ai.usage.output_tokens"], a["gen_ai.usage.total_tokens"], res.UsageMetadata.ToolUsePromptTokenCount)
			}
		})
	}
}
