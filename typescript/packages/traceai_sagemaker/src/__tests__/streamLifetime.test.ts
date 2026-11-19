import { setFlagsFromString } from "v8";
import { runInNewContext } from "vm";
import { client, ENDPOINT, instrument } from "./harness";

setFlagsFromString("--expose-gc");
const collect = runInNewContext("gc") as () => void;
const lib = "@aws-sdk/client-sagemaker-runtime" as const;
const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 5));

async function settle() {
  for (let i = 0; i < 3; i++) {
    collect();
    await tick();
  }
}

const wire = (text: string) => ({ PayloadPart: { Bytes: new TextEncoder().encode(`data: ${JSON.stringify({ choices: [{ delta: { content: text } }] })}\n`) } });

describe("stream lifetime while only the iterator is referenced", () => {
  // Opens the stream and returns nothing but the iterator, so the response object is unreachable.
  async function open(ctx: ReturnType<typeof instrument>, gate: Promise<void>) {
    async function* body() {
      yield wire("alpha-");
      await gate;
      yield wire("beta-");
      yield wire("gamma");
    }
    const c = client(ctx.lib, () => ({ Body: body(), ContentType: "text/event-stream", $metadata: {} }));
    const out = await c.send(new ctx.lib.InvokeEndpointWithResponseStreamCommand({ EndpointName: ENDPOINT, ContentType: "application/json", Body: new Uint8Array() }));
    return out.Body[Symbol.asyncIterator]() as AsyncIterator<unknown>;
  }

  it("keeps the span open through collection and ends it once, complete, after the last event", async () => {
    const ctx = instrument(lib);
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const iterator = await open(ctx, gate);
    await iterator.next();
    await settle();
    expect(ctx.spans()).toHaveLength(0);
    release();
    await iterator.next();
    await settle();
    expect(ctx.spans()).toHaveLength(0);
    await iterator.next();
    expect((await iterator.next()).done).toBe(true);
    const spans = ctx.spans();
    expect(spans).toHaveLength(1);
    expect(spans[0].attributes["aws.sagemaker.stream.incomplete"]).toBeUndefined();
    expect(spans[0].attributes["output.value"]).toBe("alpha-beta-gamma");
    expect(spans[0].attributes["gen_ai.server.time_to_first_token"]).toBeDefined();
    ctx.instrumentation.disable();
    await ctx.provider.shutdown();
  });

  it("still ends an abandoned stream once, marked incomplete", async () => {
    const ctx = instrument(lib);
    const gate = Promise.resolve();
    // The iterator lives only inside this call, so nothing references it afterwards.
    await (async () => {
      const iterator = await open(ctx, gate);
      await iterator.next();
    })();
    for (let i = 0; i < 10 && ctx.spans().length === 0; i++) await settle();
    const spans = ctx.spans();
    expect(spans).toHaveLength(1);
    expect(spans[0].attributes["aws.sagemaker.stream.incomplete"]).toBe(true);
    ctx.instrumentation.disable();
    await ctx.provider.shutdown();
  }, 30000);
});

describe("supported SDK range", () => {
  it("starts at the first supported client version", () => {
    const ctx = instrument(lib);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const definition = (ctx.instrumentation as any).init();
    expect(definition.supportedVersions).toEqual([">=3.693.0 <4.0.0"]);
    ctx.instrumentation.disable();
  });
});
