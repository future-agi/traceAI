package traceaigenai_test

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/codes"
	"google.golang.org/genai"
)

const urlContextURL = "https://fixture.invalid/retrieved-page"

func urlContextBody(index int, status string) string {
	return fmt.Sprintf(`{"candidates":[{"index":%d,"content":{"role":"model","parts":[{"text":"URL answer"}]},"finishReason":"STOP","urlContextMetadata":{"urlMetadata":[{"retrievedUrl":%q,"urlRetrievalStatus":%q}]}}],"usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":5,"totalTokenCount":15}}`, index, urlContextURL, status)
}

type urlContextEvidence struct {
	Candidates []struct {
		Index int `json:"candidate_index"`
		URLs  []struct {
			URL    string `json:"retrieved_url"`
			Status string `json:"url_retrieval_status"`
		} `json:"url_retrievals"`
	} `json:"candidates"`
}

func TestURLContextCapture(t *testing.T) {
	for _, stream := range []bool{false, true} {
		for _, status := range []string{"URL_RETRIEVAL_STATUS_SUCCESS", "URL_RETRIEVAL_STATUS_ERROR"} {
			for _, flag := range []string{"", "FI_HIDE_INPUTS", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUTS", "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_OUTPUT_TEXT"} {
				t.Run(fmt.Sprintf("stream=%t/%s/%s", stream, status, flag), func(t *testing.T) {
					qClearEnv(t)
					if flag != "" {
						t.Setenv(flag, "true")
					}
					c, _, exp := qSetup(t, func(w http.ResponseWriter, r *http.Request) {
						b, _ := io.ReadAll(r.Body)
						if !strings.Contains(string(b), `"urlContext"`) || !strings.Contains(string(b), qRequest) {
							t.Error("URL Context request changed on wire")
						}
						qReply(urlContextBody(7, status), stream)(w, r)
					}, true)
					cfg := &genai.GenerateContentConfig{Tools: []*genai.Tool{{URLContext: &genai.URLContext{}}}}
					check := func(r *genai.GenerateContentResponse) {
						t.Helper()
						b, err := json.Marshal(r)
						if err != nil || r.Text() != "URL answer" || !strings.Contains(string(b), urlContextURL) || !strings.Contains(string(b), status) {
							t.Fatalf("SDK-visible response changed: %s (%v)", b, err)
						}
					}
					// The retrieved URL occurs only in response metadata, so a URL
					// copied from input.value cannot make this assertion pass.
					if stream {
						for r, err := range c.Models.GenerateContentStream(context.Background(), "gemini-test", genai.Text(qRequest), cfg) {
							if err != nil {
								t.Fatal(err)
							}
							check(r)
						}
					} else {
						r, err := c.Models.GenerateContent(context.Background(), "gemini-test", genai.Text(qRequest), cfg)
						if err != nil {
							t.Fatal(err)
						}
						check(r)
					}
					s := qOne(t, exp)
					attrs := qAttrs(s)
					if s.Status.Code == codes.Error || attrs["error.type"] != "" || attrs["gen_ai.usage.total_tokens"] != "15" {
						t.Errorf("URL fetch outcome changed generation status/usage: %v %v", s.Status, attrs)
					}
					if flag != "" {
						if attrs[groundingKey] != "" || strings.Contains(qAll(s), urlContextURL) || strings.Contains(qAll(s), status) {
							t.Error("hidden URL retrieval evidence exported")
						}
						return
					}
					var evidence urlContextEvidence
					if err := json.Unmarshal([]byte(attrs[groundingKey]), &evidence); err != nil {
						t.Fatalf("missing URL retrieval evidence: %q (%v)", attrs[groundingKey], err)
					}
					if len(evidence.Candidates) != 1 || evidence.Candidates[0].Index != 7 || len(evidence.Candidates[0].URLs) != 1 {
						t.Fatalf("candidate-indexed retrieval evidence lost: %+v", evidence)
					}
					got := evidence.Candidates[0].URLs[0]
					if got.URL != urlContextURL || got.Status != status {
						t.Errorf("retrieval result lost: %+v", got)
					}
				})
			}
		}
	}
}

func TestURLContextStreamPreservesOutcomes(t *testing.T) {
	qClearEnv(t)
	bodies := []string{
		urlContextBody(7, "URL_RETRIEVAL_STATUS_ERROR"),
		urlContextBody(7, "URL_RETRIEVAL_STATUS_ERROR"),
		urlContextBody(2, "URL_RETRIEVAL_STATUS_SUCCESS"),
		urlContextBody(7, "URL_RETRIEVAL_STATUS_SUCCESS"),
		urlContextBody(7, "URL_RETRIEVAL_STATUS_SUCCESS"),
		`{"candidates":[{"index":7,"urlContextMetadata":{}},{"index":2,"urlContextMetadata":{"urlMetadata":[]}}]}`,
		`{"candidates":[{"index":7}],"usageMetadata":{"totalTokenCount":99}}`,
	}
	c, _, exp := qSetup(t, func(w http.ResponseWriter, r *http.Request) {
		for _, body := range bodies {
			qReply(body, true)(w, r)
		}
	}, true)
	cfg := &genai.GenerateContentConfig{Tools: []*genai.Tool{{URLContext: &genai.URLContext{}}}}
	if err := qGenerate(c, context.Background(), true, genai.Text(qRequest), cfg); err != nil {
		t.Fatal(err)
	}
	s := qOne(t, exp)
	attrs := qAttrs(s)
	var evidence urlContextEvidence
	if err := json.Unmarshal([]byte(attrs[groundingKey]), &evidence); err != nil {
		t.Fatal(err)
	}
	if len(evidence.Candidates) != 2 || evidence.Candidates[0].Index != 2 || evidence.Candidates[1].Index != 7 {
		t.Fatalf("sparse candidates lost or unsorted: %+v", evidence)
	}
	first, second := evidence.Candidates[0].URLs, evidence.Candidates[1].URLs
	if len(first) != 1 || first[0].Status != "URL_RETRIEVAL_STATUS_SUCCESS" || len(second) != 2 || second[0].Status != "URL_RETRIEVAL_STATUS_ERROR" || second[1].Status != "URL_RETRIEVAL_STATUS_SUCCESS" {
		t.Errorf("stream erased or duplicated URL outcomes: %+v", evidence)
	}
	if s.Status.Code == codes.Error || attrs["gen_ai.usage.total_tokens"] != "99" {
		t.Error("retrieval metadata changed generation status or final usage")
	}
}
