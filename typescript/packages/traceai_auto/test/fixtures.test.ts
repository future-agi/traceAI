import { readFileSync, readdirSync } from "node:fs";
import { resolve, join } from "node:path";
import { build, header, isBinary, match, RequestData, ResponseData } from "../src/wire/core";
import { gzipSync } from "node:zlib";
import { decodeResponse } from "../src/wire/encoding";
const spec = resolve(__dirname, "../../../../spec/auto-instrumentation");
const paths = readdirSync(join(spec, "fixtures")).filter(f => f.endsWith(".json")).sort();
const fixtures = paths.map(f => JSON.parse(readFileSync(join(spec, "fixtures", f), "utf8")));

test("catalog is byte-identical to shared spec", () => {
  const bytes = readFileSync(join(spec, "wire-protocols.json"));
  expect(readFileSync(resolve(__dirname, "../src/wire/wire-protocols.json"))).toEqual(bytes);
  for (const dir of ["src", "esm", "esnext"]) expect(readFileSync(resolve(__dirname, `../dist/${dir}/wire/wire-protocols.json`))).toEqual(bytes);
  expect(paths.length).toBeGreaterThanOrEqual(47);
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
  if (s.error_after_chunks !== undefined) { chunks = chunks.slice(0, s.error_after_chunks); resp.error = Object.assign(new Error("Fixture transport timeout"), { name: "TimeoutError" }); }
  resp.size = chunks.reduce((n, c) => n + c.length, 0);
  if (!isBinary(header(resp.headers, "content-type"))) resp.body = Buffer.concat(chunks);
  return resp;
}
// A8 normalization can be checked without pretending to validate A10's transport
// arrival timestamps. The later milestone pass consumes those same full fixtures.
const tokenFixtures = readdirSync(join(spec, "fixtures/round6")).filter(f => /^\d+-/.test(f) && Number(f.split("-")[0]) >= 48 && Number(f.split("-")[0]) <= 63)
  .sort().map(f => JSON.parse(readFileSync(join(spec, "fixtures/round6", f), "utf8")));
test.each(tokenFixtures.map(f => [f.name, f]))("A8 shared token-algebra fixture %s", (_name, f) => {
  const req = request(f), found = match(req.method, req.url), result = found ? build(found, req, response(f)) : undefined;
  expect(result).toBeDefined();
  for (const [key, value] of Object.entries(f.expect.attributes ?? {})) if (key.startsWith("gen_ai.usage.")) expect(result!.attributes[key]).toEqual(value);
  for (const key of f.expect.absent ?? []) if (key.startsWith("gen_ai.usage.")) expect(Object.hasOwn(result!.attributes, key)).toBe(false);
});
test.each(fixtures.map(f => [f.name, f]))("shared golden fixture %s", async (_name, f) => {
  const req = request(f), found = match(req.method, req.url), resp = response(f);
  await decodeResponse(resp, 4194304);
  const result = found ? build(found, req, resp) : undefined;
  const expected = f.expect;
  if (!expected.captured) { expect(result).toBeUndefined(); return; }
  expect(result).toBeDefined(); expect(result!.name).toBe(expected.name); expect(result!.ok).toBe(expected.status === "OK");
  if ("status_description" in expected) expect(result!.description).toBe(expected.status_description);
  for (const [key, value] of Object.entries(expected.attributes ?? {})) expect(result!.attributes[key]).toEqual(value);
  for (const key of expected.present ?? []) expect(Object.hasOwn(result!.attributes, key)).toBe(true);
  for (const key of expected.absent ?? []) expect(Object.hasOwn(result!.attributes, key)).toBe(false);
  for (const [key, values] of Object.entries<string[]>(expected.not_contains ?? {})) for (const value of values) expect(String(result!.attributes[key] ?? "")).not.toContain(value);
});
