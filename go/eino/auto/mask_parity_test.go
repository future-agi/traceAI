package auto

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"strconv"
	"strings"
	"testing"

	"github.com/future-agi/traceAI/go/auto"
	traceaieino "github.com/future-agi/traceAI/go/eino"
	"go.opentelemetry.io/otel/attribute"
)

// switches are the privacy switches, one at a time, as the two modules spell them.
// noContentKeys are the go/eino attribute keys that carry no content: identity, model, usage and error-type values.
var noContentKeys = []string{
	traceaieino.ResourceProjectName, traceaieino.ResourceProjectType, traceaieino.GenAISpanKind, traceaieino.GenAIOperationName,
	traceaieino.GenAIRequestModel, traceaieino.GenAIRequestTemperature, traceaieino.GenAIRequestTopP, traceaieino.GenAIRequestMaxTokens,
	traceaieino.GenAIRequestStopSequences, traceaieino.GenAIResponseFinishReasons, traceaieino.GenAIUsageInputTokens,
	traceaieino.GenAIUsageOutputTokens, traceaieino.GenAIUsageTotalTokens, traceaieino.GenAIUsageCacheReadTokens,
	traceaieino.GenAIUsageReasoningTokens, traceaieino.GenAIToolName, traceaieino.GenAIToolCallID, traceaieino.GenAIAgentName,
	traceaieino.GenAIRetrievalTopK, traceaieino.GenAIEmbeddingsDimension, traceaieino.ErrorType, traceaieino.SessionID, traceaieino.UserID,
	traceaieino.MimeTypeJSON, traceaieino.MimeTypeText, traceaieino.EinoComponent, traceaieino.EinoType, traceaieino.EinoName,
	traceaieino.EinoAgentInterrupted, traceaieino.EinoAgentResumed, traceaieino.EinoRetrieverDocumentCount, traceaieino.EinoEmbeddingCount,
	traceaieino.EinoIndexerDocumentCount, traceaieino.SpanKindLLM, traceaieino.SpanKindTool, traceaieino.SpanKindAgent, traceaieino.SpanKindChain,
	traceaieino.SpanKindRetriever, traceaieino.SpanKindEmbedding, traceaieino.SpanKindUnknown,
	// Cost, usage, timing and provenance of an agent turn: numbers, booleans and bounded codes.
	traceaieino.GenAICostTotal, traceaieino.GenAICostInput, traceaieino.GenAICostOutput, traceaieino.GenAIServerTimeToFirstToken, traceaieino.ToolName,
	traceaieino.FIUsageScope, traceaieino.FIUsageContribution, traceaieino.FICostKind, traceaieino.FIAgentFidelity, traceaieino.FIAgentTiming,
	traceaieino.FIAgentOutcome, traceaieino.FIAgentUsageAdditive, traceaieino.FIAgentUsagePlan, traceaieino.FIAgentUsageCacheInInput,
	traceaieino.FIAgentUsageReasoningInOutput, traceaieino.FIAgentCostSource, traceaieino.FIAgentCostEstimator, traceaieino.FIAgentGap,
	traceaieino.FIAgentTransport, traceaieino.FIAgentSourceCallID, traceaieino.FIAgentSummaryInput, traceaieino.FIAgentSummaryOutput,
	traceaieino.FIAgentSummaryTotal, traceaieino.FIAgentSummaryCacheRead, traceaieino.FIAgentSummaryCacheWrite, traceaieino.FIAgentSummaryReasoning,
	traceaieino.FIAgentSummaryCostUSD, traceaieino.FIAgentTags, traceaieino.FIAgentEvent, traceaieino.FIAgentInputCompleteness, traceaieino.FIAgentContentSource,
}

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

// Every attribute key constant go/eino declares is classified here as content-bearing (it is in contentKeys, so the
// parity test above checks the agent's mask hides it wherever go/eino's does) or as carrying no content. A key
// go/eino adds later fails this test until it is classified.
func TestEveryEinoAttributeKeyIsClassified(t *testing.T) {
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, "../attributes.go", nil, 0)
	if err != nil {
		t.Fatal(err)
	}
	classified := map[string]bool{}
	for _, k := range contentKeys() {
		classified[k] = true
	}
	// A constant that is a dot-delimited part of a content key (gen_ai.input.messages, message.content, ...) is
	// classified through the key that contains it.
	partOfContentKey := func(constant string) bool {
		for _, k := range contentKeys() {
			if k == constant || strings.HasPrefix(k, constant+".") || strings.Contains(k, "."+constant+".") || strings.HasSuffix(k, "."+constant) {
				return true
			}
		}
		return false
	}
	for _, k := range noContentKeys {
		classified[k] = true
	}
	for _, decl := range file.Decls {
		gen, ok := decl.(*ast.GenDecl)
		if !ok || gen.Tok != token.CONST {
			continue
		}
		for _, spec := range gen.Specs {
			value := spec.(*ast.ValueSpec)
			for i, name := range value.Names {
				lit, ok := value.Values[i].(*ast.BasicLit)
				if !ok || lit.Kind != token.STRING {
					continue
				}
				key, _ := strconv.Unquote(lit.Value)
				if !classified[key] && !partOfContentKey(key) {
					t.Errorf("attributes.go: %s = %q is not classified in mask_parity_test.go", name.Name, key)
				}
			}
		}
	}
}
