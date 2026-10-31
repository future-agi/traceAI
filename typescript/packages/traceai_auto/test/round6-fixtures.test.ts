import { readFileSync, readdirSync } from "node:fs";
import { resolve, join } from "node:path";
import { build, match, RequestData, ResponseData } from "../src/wire/core";
const root = resolve(__dirname, "../../../../spec/auto-instrumentation/fixtures/round6");
const fixtures = readdirSync(root).filter(name => name.endsWith(".json")).sort().map(name => JSON.parse(readFileSync(join(root, name), "utf8")));

test.each(fixtures.map(f => [f.name, f]))("A8/A10 complete shared transport-prefix fixture %s", (_name, f) => {
  const req: RequestData = { method: f.request.method, url: f.request.url, headers: f.request.headers, body: Buffer.from(JSON.stringify(f.request.json)) };
  const response = f.response;
  const encoded: Buffer[] = response.sse ? response.sse.map((event: any) => {
    if (typeof event === "string") return Buffer.from(`data: ${event}\n\n`);
    const { event: type, ...payload } = event;
    return Buffer.from(`${type ? `event: ${type}\n` : ""}data: ${JSON.stringify(payload)}\n\n`);
  }) : response.ndjson ? response.ndjson.map((item: any) => Buffer.from(JSON.stringify(item) + "\n")) : [Buffer.from(JSON.stringify(response.json))];
  const all = Buffer.concat(encoded), sizes: number[] = response.chunk_sizes ?? encoded.map(chunk => chunk.length);
  expect(sizes.reduce((sum, n) => sum + n, 0)).toBe(all.length);
  let offset = 0;
  const chunks = sizes.map(size => { const chunk = all.subarray(offset, offset + size); offset += size; return chunk; });
  const observed = chunks.slice(0, response.error_after_chunks ?? chunks.length);
  const body = Buffer.concat(observed);
  const resp: ResponseData = { status: response.status, headers: response.headers, body, size: body.length };
  if (response.error_after_chunks !== undefined) resp.error = Object.assign(new Error("Fixture transport timeout"), { name: "TimeoutError" });
  if (response.arrival_offsets_ms) {
    let end = 0; resp.arrivalTimes = [];
    for (let i = 0; i < observed.length; i++) {
      end += observed[i].length;
      if (observed[i].length) { const seconds = response.arrival_offsets_ms[i] / 1000; resp.firstChunkSeconds ??= seconds; resp.arrivalTimes.push({ end, seconds }); }
    }
  }
  const found = match(req.method, req.url), result = found ? build(found, req, resp) : undefined;
  expect(result).toBeDefined(); expect(result!.name).toBe(f.expect.name); expect(result!.ok).toBe(f.expect.status === "OK");
  for (const [key, value] of Object.entries(f.expect.attributes ?? {})) expect({ [key]: result!.attributes[key] }).toEqual({ [key]: value });
  for (const key of f.expect.absent ?? []) expect({ key, present: Object.hasOwn(result!.attributes, key) }).toEqual({ key, present: false });
});
