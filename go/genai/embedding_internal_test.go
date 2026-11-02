package traceaigenai

import "testing"

func TestEmbeddingResponseMetadata(t *testing.T) {
	for _, tt := range []struct {
		name, body       string
		count, dimension int64
		wantError        bool
	}{
		{"single", `{"embedding":{"values":[0.1,0.2]}}`, 1, 2, false},
		{"batch", `{"embeddings":[{"values":[0.1,0.2]},{"values":[0.3,0.4]}]}`, 2, 2, false},
		{"mixed", `{"embeddings":[{"values":[0.1]},{"values":[0.3,0.4]}]}`, 2, 0, false},
		{"empty vector", `{"embedding":{"values":[]}}`, 1, 0, false},
		{"empty body", ``, 0, 0, false},
		{"empty batch", `{"embeddings":[]}`, 0, 0, false},
		{"malformed", `not-json`, 0, 0, true},
		{"wrong types", `{"embedding":{"values":["private"]}}`, 0, 0, true},
		{"unknown", `{"private":"content"}`, 0, 0, false},
	} {
		t.Run(tt.name, func(t *testing.T) {
			got := map[string]int64{}
			attrs, err := embeddingResponseAttributes([]byte(tt.body))
			if (err != nil) != tt.wantError {
				t.Fatalf("decode error = %v, want error = %v", err, tt.wantError)
			}
			for _, kv := range attrs {
				got[string(kv.Key)] = kv.Value.AsInt64()
			}
			if got[EmbeddingCount] != tt.count || got[GenAIEmbeddingDimensionCount] != tt.dimension {
				t.Fatalf("metadata=%v", got)
			}
			if tt.dimension == 0 {
				if _, ok := got[GenAIEmbeddingDimensionCount]; ok {
					t.Error("invented dimension")
				}
			}
		})
	}
}

func TestEmbeddingRequestParametersDoNotCaptureArbitraryValues(t *testing.T) {
	for _, body := range []string{`not-json`, `{"content":{"parts":[{"text":"document"}]},"taskType":"private-task","outputDimensionality":-1,"private":"secret"}`} {
		p := parseEmbeddingRequest([]byte(body), TraceConfig{}, false)
		for _, kv := range p.attrs {
			if string(kv.Key) == GenAIRequestParameters {
				t.Error("invalid parameters captured")
			}
		}
	}
}
