// Compile against the independently packed public API and ADK 2.4 declarations.
// No runtime execution or provider credentials are needed.
import { BasePlugin, InMemoryRunner, LlmAgent } from "@google/adk";
import { recordGoogleADKModelError } from "@traceai/fi-instrumentation-google-adk";

class ModelErrorTelemetry extends BasePlugin {
  constructor() { super("traceai_model_errors"); }
  override onModelErrorCallback = recordGoogleADKModelError;
}

new InMemoryRunner({
  agent: new LlmAgent({name: "type_contract", model: "gemini-2.5-flash"}),
  plugins: [new ModelErrorTelemetry()],
});
