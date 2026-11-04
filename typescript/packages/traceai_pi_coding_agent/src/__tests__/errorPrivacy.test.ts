import { SpanStatusCode } from "@opentelemetry/api";
import { PiCodingAgentInstrumentation } from "../index";
import { resolveContentPolicy } from "../instrumentation";
import { PiMessage } from "../types";
import { FakeSession, PROVIDER_ERROR_TEXT, TOOL_PATH_ERROR, providerAndToolPathErrors } from "./fixtures/events";
import { exportedText, memoryProvider } from "./helpers";

const MARKER = "FAILED_MODEL_OUTPUT_PRIVATE";
const before = process.env.FI_HIDE_ERROR_DETAILS;
beforeEach(() => { delete process.env.FI_HIDE_ERROR_DETAILS; });
afterEach(() => {
  if (before === undefined) delete process.env.FI_HIDE_ERROR_DETAILS;
  else process.env.FI_HIDE_ERROR_DETAILS = before;
});

it.each([undefined, true])("output opt-in alone keeps failed tool results and model errors private (%s)", (hideErrorDetails) => {
  const { provider, exporter } = memoryProvider();
  const session = new FakeSession();
  new PiCodingAgentInstrumentation({ tracerProvider: provider, traceConfig: { hideOutputs: false, hideErrorDetails } }).instrumentSession(session);
  const events = providerAndToolPathErrors();
  for (const event of events) {
    const message = event.message as PiMessage | undefined;
    if (message?.stopReason === "error") message.content = [{ type: "text", text: MARKER }];
  }
  session.emit(events);
  const spans = exporter.getFinishedSpans();
  const failed = spans.filter((s) => s.status.code === SpanStatusCode.ERROR);
  expect(failed.length).toBeGreaterThanOrEqual(4);
  for (const span of failed) {
    expect(span.status.message).toBeUndefined();
    expect(span.attributes["error.type"]).toBeDefined();
  }
  const text = exportedText(spans);
  for (const privateText of [MARKER, "PROVIDER_ERROR_SECRET", "TOOL_PATH_SECRET", "/Users/"]) {
    expect(text).not.toContain(privateText);
  }
});

it("both opt-ins retain raw tool and provider errors", () => {
  const { provider, exporter } = memoryProvider();
  const session = new FakeSession();
  new PiCodingAgentInstrumentation({ tracerProvider: provider, traceConfig: { hideOutputs: false, hideErrorDetails: false } }).instrumentSession(session);
  session.emit(providerAndToolPathErrors());
  const text = exportedText(exporter.getFinishedSpans());
  expect(text).toContain(JSON.stringify(PROVIDER_ERROR_TEXT).slice(1, -1));
  expect(text).toContain(JSON.stringify(TOOL_PATH_ERROR).slice(1, -1));
});

it.each([undefined, "", "0", "off", "true", "invalid"])("error privacy fails closed for env %s", (value) => {
  if (value !== undefined) process.env.FI_HIDE_ERROR_DETAILS = value;
  expect(resolveContentPolicy().hideErrorDetails).toBe(true);
});

it("explicit options take precedence over error-detail environment settings", () => {
  process.env.FI_HIDE_ERROR_DETAILS = " FALSE ";
  expect(resolveContentPolicy().hideErrorDetails).toBe(false);
  expect(resolveContentPolicy({ hideErrorDetails: true }).hideErrorDetails).toBe(true);
  process.env.FI_HIDE_ERROR_DETAILS = "true";
  expect(resolveContentPolicy({ hideErrorDetails: false }).hideErrorDetails).toBe(false);
});
