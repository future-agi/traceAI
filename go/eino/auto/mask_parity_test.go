package auto

import (
	"fmt"
	"strings"
	"testing"

	"github.com/future-agi/traceAI/go/auto"
	traceaieino "github.com/future-agi/traceAI/go/eino"
	"go.opentelemetry.io/otel/attribute"
)

// switches are the privacy switches, one at a time, as the two modules spell them.
var switches = []struct {
	name string
	eino traceaieino.TraceConfig
	auto auto.TraceConfig
}{
	{"HideInputs", traceaieino.TraceConfig{HideInputs: true}, auto.TraceConfig{HideInputs: true}},
	{"HideOutputs", traceaieino.TraceConfig{HideOutputs: true}, auto.TraceConfig{HideOutputs: true}},
	{"HideInputMessages", traceaieino.TraceConfig{HideInputMessages: true}, auto.TraceConfig{HideInputMessages: true}},
	{"HideOutputMessages", traceaieino.TraceConfig{HideOutputMessages: true}, auto.TraceConfig{HideOutputMessages: true}},
	{"HideInputText", traceaieino.TraceConfig{HideInputText: true}, auto.TraceConfig{HideInputText: true}},
	{"HideOutputText", traceaieino.TraceConfig{HideOutputText: true}, auto.TraceConfig{HideOutputText: true}},
	{"HideInputImages", traceaieino.TraceConfig{HideInputImages: true}, auto.TraceConfig{HideInputImages: true}},
	{"HideLLMInvocationParameters", traceaieino.TraceConfig{HideLLMInvocationParameters: true}, auto.TraceConfig{HideLLMInvocationParameters: true}},
}

// contentKeys are the attribute keys the go/eino handler writes that carry content or content-derived values, in the
// shapes it writes them (see go/eino/mapping.go): every key a go/eino switch can hide must be hidden by the agent's
// mask too, which is the one the blank import applies.
func contentKeys() []string {
	keys := []string{
		traceaieino.InputValue, traceaieino.InputMimeType, traceaieino.OutputValue, traceaieino.OutputMimeType,
		traceaieino.GenAIToolCallArguments, traceaieino.GenAIToolCallResult, traceaieino.GenAIRetrievalQuery,
		traceaieino.GenAIRequestParameters, traceaieino.ErrorMessage,
		traceaieino.GenAIToolDefinitions + ".0." + traceaieino.ToolJSONSchema,
	}
	for _, p := range []string{traceaieino.GenAIInputMessages, traceaieino.GenAIOutputMessages} {
		m := p + ".0."
		keys = append(keys, m+traceaieino.MessageRole, m+traceaieino.MessageName, m+traceaieino.MessageToolCallID, m+traceaieino.MessageContent,
			m+traceaieino.MessageContents+".0."+traceaieino.MessageContentType, m+traceaieino.MessageContents+".0."+traceaieino.MessageContentText,
			m+traceaieino.MessageContents+".0."+traceaieino.MessageContentImage,
			m+traceaieino.MessageToolCalls+".0."+traceaieino.ToolCallID, m+traceaieino.MessageToolCalls+".0."+traceaieino.ToolCallFunctionName,
			m+traceaieino.MessageToolCalls+".0."+traceaieino.ToolCallFunctionArguments)
	}
	d := traceaieino.RetrievalDocuments + ".0."
	return append(keys, d+traceaieino.DocumentID, d+traceaieino.DocumentScore, d+traceaieino.DocumentContent)
}

func hidden(kv attribute.KeyValue, masked attribute.KeyValue, keep bool) bool {
	return !keep || masked.Value.Emit() != kv.Value.Emit()
}

// For every key and every switch, the agent's mask hides what go/eino's mask hides.
func TestAgentMaskHidesEverythingTheEinoMaskHides(t *testing.T) {
	const sample = "sample content text that must not leave when hidden"
	var table strings.Builder
	fmt.Fprintf(&table, "%-70s %-28s %s\n", "key", "switch", "go/eino | agent")
	for _, key := range contentKeys() {
		kv := attribute.String(key, sample)
		for _, s := range switches {
			em, ek := s.eino.Mask(kv)
			am, ak := s.auto.Mask(kv)
			eh, ah := hidden(kv, em, ek), hidden(kv, am, ak)
			if eh || ah {
				fmt.Fprintf(&table, "%-70s %-28s %v | %v\n", key, s.name, eh, ah)
			}
			if eh && !ah {
				t.Errorf("%s hides %q but the agent's mask does not", s.name, key)
			}
		}
	}
	t.Log("\n" + table.String())
}
