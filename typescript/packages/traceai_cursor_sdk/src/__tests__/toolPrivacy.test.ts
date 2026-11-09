import { CursorSDKInstrumentation } from "../instrumentation";
import { SDKMessageLike } from "../types";
import { drain, exportedText, memoryProvider, one } from "./helpers";

const input = "private-tool-input";
const output = "permitted-tool-output";
const failure = "private-tool-error /home/private/path";
const envKeys = ["FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_ERROR_DETAILS"];
const { provider, exporter } = memoryProvider();
let saved: (string | undefined)[];
let instrumentation: CursorSDKInstrumentation;
beforeEach(() => {
  exporter.reset();
  saved = envKeys.map(key => process.env[key]);
  envKeys.forEach(key => delete process.env[key]);
});
afterEach(() => {
  instrumentation?.unpatch();
  instrumentation?.disable();
  envKeys.forEach((key, i) => {
    if (saved[i] === undefined) delete process.env[key]; else process.env[key] = saved[i];
  });
});
afterAll(() => provider.shutdown());

const gates = [true, false].flatMap(hideInputs => [true, false].map(hideOutputs => ({ hideInputs, hideOutputs })));
const policies = ["default", "env-true", "env-false", "explicit-true", "explicit-false"] as const;
for (const source of ["stream", "step", "nested", "nested-protobuf"] as const) {
  for (const errorPolicy of policies) {
    it.each(gates)(`${source} ${errorPolicy}: tool error privacy with %j`, async ({ hideInputs, hideOutputs }) => {
      process.env.FI_HIDE_INPUTS = String(hideInputs);
      process.env.FI_HIDE_OUTPUTS = String(hideOutputs);
      const explicit = errorPolicy.startsWith("explicit-");
      const hideErrorDetails = errorPolicy.endsWith("true");
      if (errorPolicy !== "default") process.env.FI_HIDE_ERROR_DETAILS = String(explicit ? !hideErrorDetails : hideErrorDetails);
      instrumentation = new CursorSDKInstrumentation({
        tracerProvider: provider,
        traceConfig: explicit ? { hideErrorDetails } : undefined,
      });
      const failedTool = { type: "read", callId: "failed", args: { path: input }, result: { status: "error", error: { message: failure } } };
      const successfulTool = { type: "shell", callId: "ok", args: { command: input }, result: { status: "success", value: output } };
      const steps = [failedTool, successfulTool].map(message => source === "nested-protobuf" ? {
        toolCall: { callId: message.callId, [`${message.type}ToolCall`]: {
          args: message.args, result: "error" in message.result ? { error: message.result.error } : { success: message.result.value },
        } },
      } : { type: "toolCall", message });
      const events: SDKMessageLike[] = source.startsWith("nested") ? [{
        type: "tool_call", name: "task", call_id: "parent", status: "completed",
        result: { status: "success", value: { conversationSteps: steps } },
      }] : [failedTool, successfulTool].map(tool => ({
        type: "tool_call", name: tool.type, call_id: tool.callId,
        status: tool.result.status === "error" ? "error" : "completed", args: tool.args, result: tool.result,
      }));
      const agent = instrumentation.wrapAgent({ async send(_message: string, options?: { onStep?: (event: unknown) => void }) {
        if (source === "step") steps.forEach(step => options?.onStep?.({ step }));
        return {
          id: "privacy-run", status: "finished", async wait() { return { status: "finished" }; },
          async *stream() { if (source !== "step") yield* events; },
        };
      } });
      const run = await agent.send("prompt");
      await drain(run.stream()); await run.wait();
      const spans = exporter.getFinishedSpans();
      const failed = one(spans, "Cursor tool: read");
      const succeeded = one(spans, "Cursor tool: shell");
      const exported = exportedText(spans);
      const redact = hideInputs || hideOutputs || (errorPolicy !== "default" && hideErrorDetails);
      expect(failed.status.code).toBe(2);
      expect(failed.attributes["cursor.tool.status"]).toBe("error");
      if (redact) {
        expect(exported).not.toContain(failure);
        expect(failed.attributes["output.value"]).toBeUndefined();
      } else {
        expect(failed.status.message).toContain(failure);
        expect(failed.attributes["output.value"]).toContain(failure);
      }
      if (hideInputs) expect(exported).not.toContain(input); else expect(exported).toContain(input);
      if (hideOutputs) expect(succeeded.attributes["output.value"]).toBeUndefined();
      else expect(succeeded.attributes["output.value"]).toContain(output);
    });
  }
}
