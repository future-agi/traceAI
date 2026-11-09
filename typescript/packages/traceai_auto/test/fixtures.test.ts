import { readFileSync, readdirSync } from "node:fs";
import { resolve, join, relative } from "node:path";
import { build, header, isBinary, match, RequestData, ResponseData } from "../src/wire/core";
import { gzipSync } from "node:zlib";
import { decodeResponse } from "../src/wire/encoding";
import { streamingMilestones, Arrival } from "../src/wire/stream-timing";
const spec = resolve(__dirname, "../../../../spec/auto-instrumentation");
const root = join(spec, "fixtures");
// Recursive discovery (ROUND6): every golden under fixtures/, including round6/
// and any later subdirectory, goes through this one runner and its full assertions.
function walk(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap(entry => entry.isDirectory() ? walk(join(dir, entry.name)) : entry.name.endsWith(".json") ? [join(dir, entry.name)] : []);
}
const paths = walk(root).map(path => relative(root, path)).sort();
const fixtures = paths.map(path => ({ path, ...JSON.parse(readFileSync(join(root, path), "utf8")) }));
// Spec A10 fixture scope: round6 157-166 are decoded-payload recognizer tests over a
// synthetic SSE carrier, not wire-conformance cases for a real converse-stream endpoint.
const decodedPayload = (f: any) => /^round6\/1(?:5[7-9]|6[0-6])-milestone-a10-bedrock-/.test(f.path);
const wireFixtures = fixtures.filter(f => !decodedPayload(f)), decodedFixtures = fixtures.filter(decodedPayload);

test("catalog is byte-identical to shared spec", () => {
  const bytes = readFileSync(join(spec, "wire-protocols.json"));
  expect(readFileSync(resolve(__dirname, "../src/wire/wire-protocols.json"))).toEqual(bytes);
  for (const dir of ["src", "esm", "esnext"]) expect(readFileSync(resolve(__dirname, `../dist/${dir}/wire/wire-protocols.json`))).toEqual(bytes);
  expect(paths.length).toBeGreaterThanOrEqual(47);
});
test("runner discovers every spec fixture recursively (47 baseline + 293 round6)", () => {
  expect(paths.length).toBeGreaterThanOrEqual(340);
  expect(paths.filter(path => !path.includes("/")).length).toBeGreaterThanOrEqual(47);
  expect(paths.filter(path => path.startsWith("round6/")).length).toBeGreaterThanOrEqual(293);
  expect(decodedFixtures.map(f => f.path.split("/")[1].split("-")[0])).toEqual(["157", "158", "159", "160", "161", "162", "163", "164", "165", "166"]);
});
function request(f: any): RequestData {
  const s = f.request, req: RequestData = { method: s.method, url: s.url, headers: { ...s.headers } };
  if (s.unavailable) { req.unavailable = true; return req; }
  if ("json" in s) { req.headers["content-type"] ??= "application/json"; req.body = Buffer.from(JSON.stringify(s.json)); }
  else if ("text" in s) req.body = Buffer.from(s.text);
  else if (s.binary) { req.headers["content-type"] ??= s.binary.content_type; req.body = Buffer.alloc(s.binary.size); }
  else if (s.multipart) {
    const boundary = "traceaifixtureboundary", parts: Buffer[] = [];
    for (const [name, value] of Object.entries(s.multipart.fields)) parts.push(Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n${value}\r\n`));
    for (const file of s.multipart.files) parts.push(Buffer.concat([Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="${file.name}"; filename="${file.filename}"\r\nContent-Type: ${file.content_type}\r\n\r\n`), Buffer.alloc(file.size), Buffer.from("\r\n")]));
    parts.push(Buffer.from(`--${boundary}--\r\n`)); req.headers["content-type"] = `multipart/form-data; boundary=${boundary}`; req.body = Buffer.concat(parts);
  }
  return req;
}
/** Encode one chunk per JSON/SSE/NDJSON item, then apply chunk_sizes BEFORE
 * error_after_chunks (spec A8/A10: delivered transport chunks, not events), then
 * record each nonempty observed chunk's arrival offset at its last byte. */
function response(f: any): ResponseData {
  const s = f.response, resp: ResponseData = { status: s.status, headers: { ...s.headers }, size: 0 };
  let chunks: Buffer[] = [];
  if ("json" in s) chunks = [Buffer.from(JSON.stringify(s.json))];
  else if ("text" in s) chunks = [Buffer.from(s.text)];
  else if (s.binary) chunks = [Buffer.alloc(Math.floor(s.binary.size / 2), 1), Buffer.alloc(s.binary.size - Math.floor(s.binary.size / 2), 1)];
  else if (s.sse) chunks = s.sse.map((item: any) => {
    if (typeof item === "string") return Buffer.from(`data: ${item}\n\n`);
    const { event, ...payload } = item;
    return Buffer.from(`${event ? `event: ${event}\n` : ""}data: ${JSON.stringify(payload)}\n\n`);
  });
  else if (s.ndjson) chunks = s.ndjson.map((item: any) => Buffer.from(JSON.stringify(item) + "\n"));
  if (s.gzip) { chunks = [gzipSync(Buffer.concat(chunks))]; resp.headers["content-encoding"] = "gzip"; }
  if (s.chunk_sizes) {
    const all = Buffer.concat(chunks);
    expect(s.chunk_sizes.reduce((sum: number, n: number) => sum + n, 0)).toBe(all.length);
    let offset = 0;
    chunks = s.chunk_sizes.map((size: number) => { const chunk = all.subarray(offset, offset + size); offset += size; return chunk; });
  }
  if (s.error_after_chunks !== undefined) { chunks = chunks.slice(0, s.error_after_chunks); resp.error = Object.assign(new Error("Fixture transport timeout"), { name: "TimeoutError" }); }
  if (s.arrival_offsets_ms) {
    let end = 0; const arrivals: Arrival[] = [];
    chunks.forEach((chunk, i) => {
      end += chunk.length;
      if (chunk.length) { const seconds = s.arrival_offsets_ms[i] / 1000; resp.firstChunkSeconds ??= seconds; arrivals.push({ end, seconds }); }
    });
    resp.arrivalTimes = arrivals;
  }
  resp.size = chunks.reduce((n, c) => n + c.length, 0);
  if (!isBinary(header(resp.headers, "content-type"))) resp.body = Buffer.concat(chunks);
  return resp;
}
test.each(wireFixtures.map(f => [f.path, f]))("shared golden fixture %s", async (_path, f) => {
  const req = request(f), found = match(req.method, req.url), resp = response(f);
  await decodeResponse(resp, 4194304);
  const result = found ? build(found, req, resp) : undefined;
  const expected = f.expect;
  if (!expected.captured) { expect(result).toBeUndefined(); return; }
  expect(result).toBeDefined(); expect(result!.name).toBe(expected.name); expect(result!.ok).toBe(expected.status === "OK");
  if ("status_description" in expected) expect(result!.description).toBe(expected.status_description);
  for (const [key, value] of Object.entries(expected.attributes ?? {})) expect({ [key]: result!.attributes[key] }).toEqual({ [key]: value });
  for (const key of expected.present ?? []) expect({ key, present: Object.hasOwn(result!.attributes, key) }).toEqual({ key, present: true });
  for (const key of expected.absent ?? []) expect({ key, present: Object.hasOwn(result!.attributes, key) }).toEqual({ key, present: false });
  for (const [key, values] of Object.entries<string[]>(expected.not_contains ?? {})) for (const value of values) expect(String(result!.attributes[key] ?? "")).not.toContain(value);
});
const MILESTONES = ["gen_ai.response.time_to_first_chunk", "gen_ai.response.time_to_first_text"];
test.each(decodedFixtures.map(f => [f.path, f]))("decoded-payload A10 recognizer fixture %s (not a binary event-stream wire case)", (_path, f) => {
  const resp = response(f), expected = f.expect;
  // Only milestone semantics are in scope; the stream flag is a wire-endpoint property.
  expect(Object.keys(expected.attributes).filter(key => !MILESTONES.includes(key) && key !== "gen_ai.request.stream")).toEqual([]);
  expect((expected.absent as string[]).filter(key => !MILESTONES.includes(key))).toEqual([]);
  const timing = streamingMilestones(resp.body!, true, resp.arrivalTimes!, !resp.error);
  const observed: Record<string, number | undefined> = { "gen_ai.response.time_to_first_chunk": resp.firstChunkSeconds, "gen_ai.response.time_to_first_text": timing.firstText };
  for (const key of MILESTONES) {
    if (key in expected.attributes) expect({ [key]: observed[key] }).toEqual({ [key]: expected.attributes[key] });
    if (expected.absent.includes(key)) expect({ [key]: observed[key] }).toEqual({ [key]: undefined });
  }
});
