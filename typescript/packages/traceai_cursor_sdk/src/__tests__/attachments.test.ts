import { AttachmentManager, AttachmentTransportError, type AttachmentTransportV1 } from "@traceai/fi-core/attachments";
import { SpanNames } from "../attributes";
import { CursorSDKInstrumentation } from "../instrumentation";
import { AGENT_ID, fullRun } from "./fixtures/events";
import { makeFakeSdk } from "./fixtures/fakeSdk";
import { byName, drain, memoryProvider, until } from "./helpers";

// eslint-disable-next-line @typescript-eslint/no-var-requires
const { ids, policy, limits, expiry } = require("../../../fi-core/test/attachments-fixtures.cjs");

// Any FI_ATTACHMENTS_* process warning means the V1 path regressed (legacy transport or rejected wire shape).
const warnings: string[] = [];
process.on("warning", (w: Error & { code?: string }) => {
  if (String(w.code).startsWith("FI_ATTACHMENTS_")) warnings.push(String(w.code));
});

type Upload = { request: Record<string, any>; bytes?: Buffer };
/** A synthetic in-memory AttachmentTransportV1; `calls` lists wire operations in order. */
function v1Double(options: { bootstrapError?: boolean; maxUploadBytes?: number } = {}) {
  const calls: string[] = [];
  const uploads: Upload[] = [];
  let n = 0;
  const unsupported = async (): Promise<never> => { throw new AttachmentTransportError("unsupported_version"); };
  const transport = {
    async bootstrap(body: { capture_session_id: string }) {
      calls.push("bootstrap");
      if (options.bootstrapError) throw new AttachmentTransportError("credential_scope_unsupported");
      return { schema_version: 1, scope: { organization_id: ids[0], workspace_id: ids[1], project_id: ids[2] },
        capture_session_id: body.capture_session_id, policy,
        limits: { ...limits, ...(options.maxUploadBytes ? { max_upload_bytes: options.maxUploadBytes } : {}) },
        expires_at: expiry(), transfer_modes: ["authenticated_stream"] };
    },
    async initialize(_s: unknown, body: string) {
      calls.push("initialize");
      const request = JSON.parse(body);
      uploads.push({ request });
      return { upload_id: `30000000-0000-4000-8000-${(++n).toString(16).padStart(12, "0")}`, attachment_id: request.attachment_id,
        occurrence_id: request.occurrence_id, state: "pending_upload", reserved_bytes: request.declared_bytes, expires_at: expiry(),
        transfer_mode: "authenticated_stream", write_capability: "synthetic-write-capability" };
    },
    async putContent(_s: unknown, upload: { attachment_id: string }, source: Uint8Array | Blob) {
      calls.push("putContent");
      const bytes = source instanceof Uint8Array ? Buffer.from(source) : Buffer.from(await source.arrayBuffer());
      uploads.find((x) => x.request.attachment_id === upload.attachment_id)!.bytes = bytes;
    },
    async complete() { calls.push("complete"); return "validating" as const; },
    async abort() { calls.push("abort"); },
    renewSession: unsupported, renewUpload: unsupported, uploadStatus: unsupported,
    putManifest: unsupported, submitImport: unsupported, importStatus: unsupported,
  };
  // Synthetic JSON shapes, checked by the core at runtime; the cast only skips literal-type inference.
  return { transport: transport as unknown as AttachmentTransportV1, calls, uploads };
}

const PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==";
const { provider, exporter } = memoryProvider();
const cleanup: Array<() => unknown> = [];

async function setup(options: {
  hideInputs?: boolean; packageDefault?: boolean; enabled?: boolean; manager?: false; bootstrapError?: boolean;
  traceConfig?: Record<string, boolean>; maxUploadBytes?: number;
} = {}) {
  const broker = v1Double({ bootstrapError: options.bootstrapError, maxUploadBytes: options.maxUploadBytes });
  const uploads = broker.uploads;
  const manager = new AttachmentManager({
    enabled: options.enabled ?? true, projectName: "cursor", projectType: "observe", transport: broker.transport,
  });
  cleanup.push(() => manager.shutdown());
  await manager.ready();
  const instrumentation = new CursorSDKInstrumentation({
    tracerProvider: provider,
    // The package hides inputs by default; tests opt in unless checking that default.
    traceConfig: options.packageDefault ? undefined : { hideInputs: options.hideInputs ?? false, ...options.traceConfig },
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
    expect(warnings).toEqual([]);
  });

  it("preflights before any base64 read, then re-preflights once at the exact size under a small server cap", async () => {
    const exact = Buffer.from(PNG, "base64").length;
    const { sdk, manager, uploads } = await setup({ maxUploadBytes: exact });
    const sizes: number[] = [];
    const preflight = manager.preflight.bind(manager);
    let readsBeforePreflight = 0;
    let preflights = 0;
    manager.preflight = (target: Parameters<typeof preflight>[0], opts: Parameters<typeof preflight>[1]) => {
      preflights++; sizes.push(opts.maxBytes); return preflight(target, opts);
    };
    await send(sdk, { text: "describe", images: [{ get data() { if (!preflights) readsBeforePreflight++; return PNG; }, mimeType: "image/png" }] });
    await manager.flush();
    expect(readsBeforePreflight).toBe(0);
    expect(sizes).toEqual([256 * 1024, exact]);
    expect(uploads).toHaveLength(1);
    expect(uploads[0].bytes).toEqual(Buffer.from(PNG, "base64"));
    expect(manager.status().openTickets).toBe(0);
  });

  it.each([
    ["package default hides inputs", { packageDefault: true }],
    ["explicit hideInputs", { hideInputs: true }],
    ["attachments disabled", { enabled: false }],
    ["no manager passed", { manager: false as const }],
    ["unsupported credential scope", { bootstrapError: true }],
    // Deny-wins: the instrumentation's own flags restrict a provider manager that has none of them.
    ["instrumentation hideInputImages", { traceConfig: { hideInputImages: true } }],
    ["instrumentation piiRedaction", { traceConfig: { piiRedaction: true } }],
    ["instrumentation hideInputText", { traceConfig: { hideInputText: true } }],
    ["instrumentation hideInputMessages", { traceConfig: { hideInputMessages: true } }],
  ])("%s: no image bytes read and no upload", async (_name, options) => {
    const { sdk, manager, uploads, broker } = await setup(options);
    let reads = 0;
    await send(sdk, { text: "describe", images: [{ get data() { reads++; return PNG; }, mimeType: "image/png" }] });
    await manager.flush();
    expect(reads).toBe(0);
    expect(uploads).toHaveLength(0);
    expect(broker.calls.filter((c: string) => c !== "bootstrap")).toEqual([]);
  });

  it.each(["FI_HIDE_INPUT_IMAGES", "FI_PII_REDACTION", "FI_HIDE_INPUT_TEXT", "FI_HIDE_INPUT_MESSAGES"])(
    "%s=true in the environment denies prompt images", async (key) => {
      const saved = process.env[key];
      process.env[key] = "true";
      try {
        const { sdk, manager, uploads } = await setup();
        let reads = 0;
        await send(sdk, { text: "describe", images: [{ get data() { reads++; return PNG; }, mimeType: "image/png" }] });
        await manager.flush();
        expect(reads).toBe(0);
        expect(uploads).toHaveLength(0);
      } finally {
        if (saved === undefined) delete process.env[key]; else process.env[key] = saved;
      }
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
    // Every refused image released its reservation.
    expect(manager.status().openTickets).toBe(0);
    expect(manager.status().ownedBytes).toBe(0);
  });
});
