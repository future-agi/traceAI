package traceaigenai_test

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"reflect"
	"strings"
	"testing"

	traceaigenai "github.com/future-agi/traceAI/go/genai"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"google.golang.org/genai"
)

// Synthetic customer-authored text, including values outside description fields.
const toolPrivateDescription = "synthetic-private-tool-description"
const toolPrivateParameter = "synthetic-private-nested-schema"
const toolPrivateEnum = "synthetic-private-enum-value"

func TestToolDefinitionPrivacy(t *testing.T) {
	for _, stream := range []bool{false, true} {
		for _, rawSchema := range []bool{false, true} {
			for _, tc := range []struct {
				name  string
				flags []string
				hide  bool
			}{
				{"default", nil, false},
				{"explicit_false", []string{"false"}, false},
				{"inputs", []string{"FI_HIDE_INPUTS"}, true},
				{"input_text", []string{"FI_HIDE_INPUT_TEXT"}, true},
				{"input_messages", []string{"FI_HIDE_INPUT_MESSAGES"}, true},
				{"invocations", []string{"FI_HIDE_LLM_INVOCATION_PARAMETERS"}, true},
				{"inputs_and_invocations", []string{"FI_HIDE_INPUTS", "FI_HIDE_LLM_INVOCATION_PARAMETERS"}, true},
				{"all_content", []string{"FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT", "FI_HIDE_INPUT_IMAGES", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_LLM_INVOCATION_PARAMETERS", "FI_HIDE_ERROR_DETAILS"}, true},
				{"outputs", []string{"FI_HIDE_OUTPUTS"}, false},
				{"output_text", []string{"FI_HIDE_OUTPUT_TEXT"}, false},
				{"output_messages", []string{"FI_HIDE_OUTPUT_MESSAGES"}, false},
				{"images", []string{"FI_HIDE_INPUT_IMAGES"}, false},
				{"errors", []string{"FI_HIDE_ERROR_DETAILS"}, false},
			} {
				t.Run(fmt.Sprintf("stream=%t/raw_schema=%t/%s", stream, rawSchema, tc.name), func(t *testing.T) {
					qClearEnv(t)
					for _, flag := range tc.flags {
						if flag == "false" {
							for _, key := range []string{"FI_HIDE_INPUTS", "FI_HIDE_INPUT_TEXT", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_LLM_INVOCATION_PARAMETERS"} {
								t.Setenv(key, "false")
							}
						} else {
							t.Setenv(flag, "true")
						}
					}
					fd := &genai.FunctionDeclaration{Name: "lookup", Description: toolPrivateDescription}
					schemaKey := "parameters"
					if rawSchema {
						schemaKey = "parametersJsonSchema"
						fd.ParametersJsonSchema = map[string]any{"type": "object", "required": []string{"account"}, "properties": map[string]any{
							"account": map[string]any{"type": "string", "description": toolPrivateParameter, "enum": []string{toolPrivateEnum}},
						}}
					} else {
						fd.Parameters = &genai.Schema{Type: genai.TypeObject, Required: []string{"account"}, Properties: map[string]*genai.Schema{
							"account": {Type: genai.TypeString, Description: toolPrivateParameter, Enum: []string{toolPrivateEnum}},
						}}
					}
					cfg := &genai.GenerateContentConfig{Tools: []*genai.Tool{{FunctionDeclarations: []*genai.FunctionDeclaration{fd}}}}
					c, _, exp := qSetup(t, func(w http.ResponseWriter, r *http.Request) {
						body, err := io.ReadAll(r.Body)
						if err != nil {
							t.Error(err)
						}
						for _, want := range []string{toolPrivateDescription, toolPrivateParameter, toolPrivateEnum, `"lookup"`, `"required":["account"]`, `"` + schemaKey + `"`} {
							if !strings.Contains(string(body), want) {
								t.Errorf("privacy changed provider request: missing %s", want)
							}
						}
						qReply(qBody, stream)(w, r)
					}, true)
					if err := qGenerate(c, context.Background(), stream, genai.Text(qRequest), cfg); err != nil {
						t.Fatal(err)
					}
					s := qOne(t, exp)
					attrs, all := qAttrs(s), qAll(s)
					if s.EndTime.IsZero() || s.Status.Code == codes.Error {
						t.Errorf("successful span did not end normally: %+v", s.Status)
					}
					for key, want := range map[string]string{"gen_ai.request.model": "gemini-test", "gen_ai.provider.name": "google", "gen_ai.usage.input_tokens": "10", "gen_ai.usage.output_tokens": "5", "gen_ai.usage.total_tokens": "15"} {
						if attrs[key] != want {
							t.Errorf("safe telemetry %s=%q want=%q", key, attrs[key], want)
						}
					}
					definition := attrs["gen_ai.tool.definitions.0.tool.json_schema"]
					if tc.hide && definition != `{"name":"lookup"}` {
						t.Errorf("hidden declaration must retain only function identity: %q", definition)
					}
					for _, marker := range []string{toolPrivateDescription, toolPrivateParameter, toolPrivateEnum} {
						if strings.Contains(all, marker) == tc.hide {
							t.Errorf("tool declaration marker %s: hide=%t", marker, tc.hide)
						}
					}
					if !tc.hide {
						var got struct {
							Name       string `json:"name"`
							Parameters struct {
								Type       string   `json:"type"`
								Required   []string `json:"required"`
								Properties map[string]struct {
									Type string `json:"type"`
								} `json:"properties"`
							} `json:"parameters"`
						}
						if err := json.Unmarshal([]byte(definition), &got); err != nil {
							t.Fatal(err)
						}
						if got.Name != "lookup" || !strings.EqualFold(got.Parameters.Type, "object") || !strings.EqualFold(got.Parameters.Properties["account"].Type, "string") || !reflect.DeepEqual(got.Parameters.Required, []string{"account"}) {
							t.Errorf("default tool identity/schema semantics changed: %s", definition)
						}
					}
					if tc.name == "all_content" {
						for _, marker := range []string{qRequest, qAnswer} {
							if strings.Contains(all, marker) {
								t.Errorf("hidden conversation escaped: %s", marker)
							}
						}
					}
				})
			}
		}
	}
}

func TestToolDefinitionPrivacyMask(t *testing.T) {
	for _, cfg := range []traceaigenai.TraceConfig{
		{HideInputs: true}, {HideInputText: true}, {HideInputMessages: true}, {HideLLMInvocationParameters: true},
	} {
		for _, key := range []string{traceaigenai.GenAIToolDefinitions, traceaigenai.GenAIToolDefinitions + ".0.tool.json_schema", traceaigenai.GenAIToolDefinitions + ".15.tool.json_schema"} {
			if _, keep := cfg.Mask(attribute.String(key, toolPrivateDescription)); keep {
				t.Errorf("%+v kept %s", cfg, key)
			}
		}
		key := traceaigenai.GenAIToolDefinitions + ".0.tool.json_schema"
		for _, value := range []string{
			`{"name":"lookup","description":"private","parameters":{"type":"object"}}`,
			`{"name":"lookup","unknown":"private","parametersJsonSchema":{"enum":["private"]}}`,
		} {
			got, keep := cfg.Mask(attribute.String(key, value))
			if !keep || got.Value.AsString() != `{"name":"lookup"}` {
				t.Errorf("%+v lost identity or retained declaration content: %+v", cfg, got)
			}
			if again, keep := cfg.Mask(got); !keep || again != got {
				t.Errorf("%+v identity filtering is not idempotent", cfg)
			}
		}
		for _, value := range []string{`{`, `null`, `[]`, `{"description":"private"}`, `{"name":12}`, `{"name":"lookup","description":`} {
			if _, keep := cfg.Mask(attribute.String(key, value)); keep {
				t.Errorf("%+v kept invalid declaration %s", cfg, value)
			}
		}
		if _, keep := cfg.Mask(attribute.Int(key, 1)); keep {
			t.Errorf("%+v kept non-string declaration", cfg)
		}
		// Do not turn a namespace rule into an arbitrary substring filter or hide
		// unrelated identity attributes used by manually instrumented tool spans.
		for _, key := range []string{traceaigenai.GenAIToolName, traceaigenai.GenAIRequestModel, traceaigenai.GenAIToolDefinitions + "_count", "other." + traceaigenai.GenAIToolDefinitions} {
			kv := attribute.String(key, "lookup")
			if got, keep := cfg.Mask(kv); !keep || got != kv {
				t.Errorf("%+v changed safe attribute %s", cfg, key)
			}
		}
	}
}
