import { AttachmentManager, AttachmentTransportError } from "@traceai/fi-core/attachments";
import { SpanNames } from "../attributes";
import { CursorSDKInstrumentation } from "../instrumentation";
import { AGENT_ID, fullRun } from "./fixtures/events";
import { makeFakeSdk } from "./fixtures/fakeSdk";
import { byName, drain, memoryProvider, until } from "./helpers";

// eslint-disable-next-line @typescript-eslint/no-var-requires
const { fake } = require("../../../fi-core/test/attachments-fixtures.cjs");

const PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==";
const { provider, exporter } = memoryProvider();
const cleanup: Array<() => unknown> = [];

async function setup(options: { hideInputs?: boolean; packageDefault?: boolean; enabled?: boolean; manager?: false; bootstrapError?: boolean } = {}) {
  const broker = fake(options.bootstrapError ? {
    bootstrap: async () => { throw new AttachmentTransportError("credential_scope_unsupported"); },
  } : {});
  const uploads: Array<{ request: Record<string, any>; bytes?: Buffer }> = [];
  const transport = {
    ...broker.transport,
    async initialize(s: unknown, request: Record<string, any>, signal: AbortSignal) {
      uploads.push({ request });
      return broker.transport.initialize(s, request, signal);
    },
    async upload(_s: unknown, u: { attachment_id: string }, data: Uint8Array) {
      uploads.find((x) => x.request.attachment_id === u.attachment_id)!.bytes = Buffer.from(data);
    },
  };
  const manager = new AttachmentManager({
    enabled: options.enabled ?? true, projectName: "cursor", projectType: "observe", transport,
  });
  cleanup.push(() => manager.shutdown());
  await manager.ready();
  const instrumentation = new CursorSDKInstrumentation({
    tracerProvider: provider,
    // The package hides inputs by default; tests opt in unless checking that default.
    traceConfig: options.packageDefault ? undefined : { hideInputs: options.hideInputs ?? false },
    attachments: options.manager === false ? undefined : manager,
    runEndGraceMs: 30,
  });
  cleanup.push(() => { instrumentation.unpatch(); instrumentation.disable(); });
  const sdk = makeFakeSdk({ runs: [fullRun()] });
  expect(instrumentation.manuallyInstrument(sdk)).toBe(true);
  return { sdk, manager, uploads, broker };
}

async function send(sdk: ReturnType<typeof makeFakeSdk>, message: unknown) {
  const agent = await sdk.Agent.create({ model: { id: "composer-2" }, local: { cwd: "/tmp" }, agentId: AGENT_ID });
  const run = await agent.send(message as never);
  await drain(run.stream());
  await run.wait();
  await until(() => byName(exporter.getFinishedSpans(), SpanNames.RUN).length === 1);
  return byName(exporter.getFinishedSpans(), SpanNames.RUN)[0];
}

const savedHide = process.env.FI_HIDE_INPUTS;
beforeEach(() => { exporter.reset(); delete process.env.FI_HIDE_INPUTS; });
afterAll(() => { if (savedHide !== undefined) process.env.FI_HIDE_INPUTS = savedHide; });
afterEach(async () => { while (cleanup.length) await cleanup.pop()!(); });

describe("Cursor SDK inline image attachments", () => {
  it("uploads inline { data, mimeType } images byte-exact on the run span; URL images are never read", async () => {
    const { sdk, manager, uploads } = await setup();
    let urlReads = 0;
    const span = await send(sdk, {
      text: "describe",
      images: [
        { data: PNG, mimeType: "image/png" },
        { get url() { urlReads++; return "https://example.invalid/signed-secret"; } },
        { data: PNG, mimeType: "image/svg+xml" },
      ],
    });
    await manager.flush();
    expect(uploads).toHaveLength(1);
    expect(uploads[0].bytes).toEqual(Buffer.from(PNG, "base64"));
    expect(uploads[0].request.association.parent.span_id).toBe(span.spanContext().spanId);
    expect(uploads[0].request.direction).toBe("input");
    expect(uploads[0].request.purpose).toBe("model_input");
    expect(uploads[0].request.part_index).toBe(0);
    expect(String(span.attributes["fi.attachment.refs"])).toContain(uploads[0].request.occurrence_id);
    expect(JSON.stringify(span.attributes)).not.toContain(PNG);
    expect(urlReads).toBe(0);
  });

  it.each([
    ["package default hides inputs", { packageDefault: true }],
    ["explicit hideInputs", { hideInputs: true }],
    ["attachments disabled", { enabled: false }],
    ["no manager passed", { manager: false as const }],
    ["unsupported credential scope", { bootstrapError: true }],
  ])("%s: no image bytes read and no upload", async (_name, options) => {
    const { sdk, manager, uploads, broker } = await setup(options);
    let reads = 0;
    await send(sdk, { text: "describe", images: [{ get data() { reads++; return PNG; }, mimeType: "image/png" }] });
    await manager.flush();
    expect(reads).toBe(0);
    expect(uploads).toHaveLength(0);
    expect(broker.calls.filter((c: string) => c !== "bootstrap")).toEqual([]);
  });

  it("malformed or oversized inline data fails closed without affecting the run", async () => {
    const { sdk, manager, uploads } = await setup();
    const span = await send(sdk, {
      text: "describe",
      images: [
        { data: "data:image/png;base64," + PNG, mimeType: "image/png" },
        { data: Buffer.alloc(256 * 1024 + 1).toString("base64"), mimeType: "image/png" },
        { get data() { throw new Error("host accessor"); }, mimeType: "image/png" },
      ],
    });
    await manager.flush();
    expect(uploads).toHaveLength(0);
    expect(span.attributes["cursor.input.image_count"]).toBe(3);
  });
});
