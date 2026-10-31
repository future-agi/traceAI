package traceaiopenai

import "encoding/json"

// responseEnvelope intentionally omits opaque reasoning, annotations, hosted
// tool payloads, file/audio bytes and other provider metadata.
type responseEnvelope struct {
	ID     string         `json:"id"`
	Model  string         `json:"model"`
	Status string         `json:"status"`
	Output []responseItem `json:"output"`
	Usage  *struct {
		InputTokens        *int64 `json:"input_tokens"`
		OutputTokens       *int64 `json:"output_tokens"`
		TotalTokens        *int64 `json:"total_tokens"`
		InputTokensDetails *struct {
			CachedTokens *int64 `json:"cached_tokens"`
		} `json:"input_tokens_details"`
		OutputTokensDetails *struct {
			ReasoningTokens *int64 `json:"reasoning_tokens"`
		} `json:"output_tokens_details"`
	} `json:"usage"`
	Error             json.RawMessage `json:"error"`
	IncompleteDetails struct {
		Reason string `json:"reason"`
	} `json:"incomplete_details"`
}

func (a *accumulator) addJSON(b []byte, op operation) {
	switch op {
	case opResponses:
		var response responseEnvelope
		if json.Unmarshal(b, &response) == nil {
			a.addResponse(response, true)
		}
	case opEmbeddings:
		// Do not deserialize or record embedding vectors.
		var response struct {
			Model string `json:"model"`
			Usage *usage `json:"usage"`
		}
		if json.Unmarshal(b, &response) == nil {
			a.model, a.usage = response.Model, response.Usage
		}
	default:
		a.addCompletion(b)
	}
}

func (a *accumulator) addResponse(response responseEnvelope, snapshot bool) {
	if response.ID != "" {
		a.id = response.ID
	}
	if response.Model != "" {
		a.model = response.Model
	}
	if u := response.Usage; u != nil {
		a.usage = &usage{PromptTokens: u.InputTokens, CompletionTokens: u.OutputTokens, TotalTokens: u.TotalTokens,
			PromptTokensDetails: u.InputTokensDetails, CompletionTokensDetails: u.OutputTokensDetails}
	}
	if snapshot && response.Output != nil {
		// Terminal responses contain full output, not deltas. Replacing the
		// accumulator prevents counting content/tool arguments twice.
		a.choices = map[int]*choiceAcc{}
		for i, item := range response.Output {
			a.addResponseItem(i, item)
		}
	}
	if len(response.Error) > 0 && string(response.Error) != "null" {
		e := parseAPIError(response.Error)
		a.streamErr = &e
	} else if response.Status == "failed" || response.Status == "incomplete" {
		a.streamErr = &apiError{Type: response.Status, Code: response.IncompleteDetails.Reason}
	}
}

func (a *accumulator) addResponseItem(index int, item responseItem) {
	switch item.Type {
	case "message":
		c := &choiceAcc{role: item.Role, calls: map[int]*callAcc{}}
		var parts []struct {
			Type    string `json:"type"`
			Text    string `json:"text"`
			Refusal string `json:"refusal"`
		}
		if json.Unmarshal(item.Content, &parts) == nil {
			for _, part := range parts {
				switch part.Type {
				case "output_text":
					c.content.WriteString(part.Text)
					c.hasContent = true
				case "refusal":
					c.content.WriteString(part.Refusal)
					c.hasContent = true
				}
			}
		}
		a.choices[index] = c
	case "function_call":
		c := &choiceAcc{role: "assistant", calls: map[int]*callAcc{}}
		c.addDelta(outMessage{ToolCalls: []toolCall{item.call()}}, false)
		a.choices[index] = c
	}
}

// addResponseEvent returns true only for terminal events. Unlike Chat streams,
// Responses do not need a [DONE] sentinel. The HTTP body may remain open after
// response.completed, response.failed or response.incomplete.
func (a *accumulator) addResponseEvent(b []byte) bool {
	var event struct {
		Type        string           `json:"type"`
		OutputIndex int              `json:"output_index"`
		Delta       string           `json:"delta"`
		Item        responseItem     `json:"item"`
		Response    responseEnvelope `json:"response"`
	}
	if json.Unmarshal(b, &event) != nil {
		return false
	}
	switch event.Type {
	case "response.created", "response.in_progress":
		a.addResponse(event.Response, false)
	case "response.output_item.added", "response.output_item.done":
		a.addResponseItem(event.OutputIndex, event.Item)
	case "response.output_text.delta", "response.refusal.delta":
		c := a.choice(event.OutputIndex)
		c.content.WriteString(event.Delta)
		c.hasContent = true
	case "response.function_call_arguments.delta":
		var call toolCall
		call.Function.Arguments = event.Delta
		a.choice(event.OutputIndex).addDelta(outMessage{ToolCalls: []toolCall{call}}, true)
	case "response.completed", "response.failed", "response.incomplete":
		// The event type is authoritative even if a compatible endpoint omits
		// status from the embedded response.
		if event.Type != "response.completed" && event.Response.Status == "" {
			event.Response.Status = event.Type[len("response."):]
		}
		a.addResponse(event.Response, true)
		return true
	case "error":
		e := parseAPIError(b)
		a.streamErr = &e
		return true
	}
	return false
}
