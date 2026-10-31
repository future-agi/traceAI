package traceaigenai

import (
	"encoding/json"
	"strconv"

	"go.opentelemetry.io/otel/attribute"
)

// Text-only documents use at most two flattened attributes each. Keep this
// preview within half of OTel's default 128-attribute span budget, leaving room
// for aggregate input, parameters, context and response/error metadata. The
// privacy-filtered input.value still contains every document in the batch.
const maxEmbeddingMessages = 32

// embeddingRequest covers embedContent and each item of batchEmbedContents.
// Title, file bytes and unknown fields are deliberately not retained.
type embeddingRequest struct {
	Content              content `json:"content"`
	TaskType             string  `json:"taskType"`
	OutputDimensionality int     `json:"outputDimensionality"`
}

func parseEmbeddingRequest(body []byte, tc TraceConfig, batch bool) parsedRequest {
	var requests []embeddingRequest
	if batch {
		var r struct {
			Requests []embeddingRequest `json:"requests"`
		}
		if json.Unmarshal(body, &r) != nil {
			return parsedRequest{}
		}
		requests = r.Requests
	} else {
		var r embeddingRequest
		if json.Unmarshal(body, &r) != nil {
			return parsedRequest{}
		}
		requests = []embeddingRequest{r}
	}
	var p parsedRequest
	var messages []content
	var params []map[string]any
	hasParams := false
	for i, r := range requests {
		// Embedding input capture is text-only. Do not copy media, tool data,
		// thoughts or opaque provider fields from multimodal content.
		message := content{Role: "user"}
		for _, item := range r.Content.Parts {
			if item.Text != nil && !item.Thought {
				text := *item.Text
				if tc.HideInputText {
					text = RedactedValue
				}
				message.Parts = append(message.Parts, part{Text: &text})
			}
		}
		messages = append(messages, message)
		if i < maxEmbeddingMessages {
			p.attrs = append(p.attrs, messageAttributes(GenAIInputMessages+"."+strconv.Itoa(i)+".", message, "user", false)...)
		}
		param := map[string]any{}
		// Task types are an API enum. Never export an arbitrary user string.
		switch r.TaskType {
		case "RETRIEVAL_QUERY", "RETRIEVAL_DOCUMENT", "SEMANTIC_SIMILARITY", "CLASSIFICATION", "CLUSTERING", "QUESTION_ANSWERING", "FACT_VERIFICATION", "CODE_RETRIEVAL_QUERY":
			param["taskType"] = r.TaskType
		}
		if r.OutputDimensionality > 0 {
			param["outputDimensionality"] = r.OutputDimensionality
		}
		hasParams = hasParams || len(param) > 0
		params = append(params, param)
	}
	if hasParams {
		b, _ := json.Marshal(map[string]any{"requests": params})
		p.attrs = append(p.attrs, attribute.String(GenAIRequestParameters, string(b)))
	}
	// Use the same input attributes as generation. The final TraceConfig
	// filter redacts input.value and drops structured messages as requested.
	if v := conversationJSON(messages, "user", tc, false); v != "" {
		p.attrs = append(p.attrs, attribute.String(InputValue, v), attribute.String(InputMimeType, MimeTypeJSON))
	}
	return p
}

// embeddingResponseAttributes records only counts, never vector values. A
// dimension is reported only when all returned vectors have the same size.
// Gemini Developer API does not return token usage for these endpoints.
func embeddingResponseAttributes(body []byte) []attribute.KeyValue {
	type embedding struct {
		Values []float64 `json:"values"`
	}
	var r struct {
		Embedding  *embedding  `json:"embedding"`
		Embeddings []embedding `json:"embeddings"`
	}
	if json.Unmarshal(body, &r) != nil {
		return nil
	}
	embeddings := r.Embeddings
	if r.Embedding != nil {
		embeddings = []embedding{*r.Embedding}
	}
	if len(embeddings) == 0 {
		return nil
	}
	attrs := []attribute.KeyValue{attribute.Int(EmbeddingCount, len(embeddings))}
	dim := len(embeddings[0].Values)
	for _, e := range embeddings[1:] {
		if len(e.Values) != dim {
			return attrs
		}
	}
	if dim > 0 {
		attrs = append(attrs, attribute.Int(GenAIEmbeddingDimensionCount, dim))
	}
	return attrs
}
