package traceaigenai

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"unicode/utf8"
)

func TestURLContextAllowListAndEmptyMetadata(t *testing.T) {
	for _, metadata := range []string{`null`, `{}`, `{"urlMetadata":null}`, `{"urlMetadata":[null,{}, {"urlRetrievalStatus":"URL_RETRIEVAL_STATUS_ERROR"}]}`} {
		a := newAccumulator()
		a.addResponse([]byte(`{"candidates":[{"urlContextMetadata":` + metadata + `}]}`))
		if a.decodeFailed || a.grounding.json() != "" {
			t.Errorf("empty metadata invented retrieval evidence: %s", metadata)
		}
	}
	for _, status := range []string{"", "URL_RETRIEVAL_STATUS_UNSPECIFIED", "URL_RETRIEVAL_STATUS_SUCCESS", "URL_RETRIEVAL_STATUS_ERROR", "URL_RETRIEVAL_STATUS_PAYWALL", "URL_RETRIEVAL_STATUS_UNSAFE", "synthetic-private-unknown"} {
		t.Run(status, func(t *testing.T) {
			a := newAccumulator()
			body := fmt.Sprintf(`{"candidates":[{"urlContextMetadata":{"unknown":"synthetic-private-context","urlMetadata":[null,{}, {"retrievedUrl":"https://fixture.invalid/source","urlRetrievalStatus":%q,"text":"synthetic-private-document"}]}}]}`, status)
			a.addResponse([]byte(body))
			if a.decodeFailed || len(a.grounding.Candidates) != 1 || len(a.grounding.Candidates[0].URLRetrievals) != 1 {
				t.Fatalf("URL result lost: %s", a.grounding.json())
			}
			got := a.grounding.Candidates[0].URLRetrievals[0]
			want := status
			if status == "synthetic-private-unknown" {
				want = ""
			}
			if got.URL != "https://fixture.invalid/source" || got.Status != want {
				t.Errorf("URL/status changed: %+v", got)
			}
			if strings.Contains(a.grounding.json(), "synthetic-private") {
				t.Error("opaque metadata or unknown status exported")
			}
		})
	}
}

func TestURLContextBoundsAcrossChunks(t *testing.T) {
	a := newAccumulator()
	a.stream = true
	for i := 0; i < 40; i++ {
		for candidate := 0; candidate < 6; candidate++ {
			body, _ := json.Marshal(map[string]any{"candidates": []any{map[string]any{
				"index": candidate,
				"urlContextMetadata": map[string]any{"urlMetadata": []any{map[string]any{
					"retrievedUrl":       fmt.Sprintf("https://fixture.invalid/%d/%s", i, strings.Repeat("界", 500)),
					"urlRetrievalStatus": "URL_RETRIEVAL_STATUS_ERROR",
				}}},
			}}})
			a.addResponse(body)
			a.addResponse(body) // Cumulative metadata does not exhaust the budget twice.
		}
	}
	a.addResponse([]byte(`{"candidates":[{"index":0,"urlContextMetadata":{"urlMetadata":[]}}]}`))
	if a.decodeFailed || len(a.grounding.Candidates) != maxGroundingCandidates || !a.grounding.Truncated {
		t.Fatalf("candidate evidence not bounded: %s", a.grounding.json())
	}
	for _, c := range a.grounding.Candidates {
		if len(c.URLRetrievals) != maxURLRetrievals || !c.Truncated {
			t.Errorf("URL results not bounded for candidate %d", c.Index)
		}
		for i, result := range c.URLRetrievals {
			if !strings.HasPrefix(result.URL, fmt.Sprintf("https://fixture.invalid/%d/", i)) || result.Status != "URL_RETRIEVAL_STATUS_ERROR" {
				t.Errorf("stream erased or duplicated retrieval outcome: %+v", result)
			}
			if len(result.URL) > maxMetadataStringBytes || !utf8.ValidString(result.URL) || strings.ContainsRune(result.URL, utf8.RuneError) {
				t.Error("URL string limit exceeded or corrupted UTF-8")
			}
		}
	}
	if value := a.grounding.json(); !json.Valid([]byte(value)) || len(value) > 50_000 {
		t.Error("URL retrieval evidence is invalid or unbounded")
	}
}
