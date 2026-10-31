import { spawnSync } from "node:child_process";
import { resolve } from "node:path";
import { build, match } from "../src/wire/core";
import { streamFrames, streamingMilestones } from "../src/wire/stream-timing";

function child(mode: string, source: boolean, limit = 128) {
  const env = { ...process.env };
  for (const key of Object.keys(env)) if (/^(?:FI_|OTEL_)/.test(key) || key === "NODE_OPTIONS") delete env[key];
  Object.assign(env, { MODE: mode, LIMIT: String(limit), VERIFY_SOURCE: source ? "1" : "0", FI_PROFILE: "traceai-s6-synthetic-missing-profile", TS_NODE_PROJECT: resolve(__dirname, "../tsconfig.json") });
  const result = spawnSync(process.execPath, [...(source ? ["-r", require.resolve("ts-node/register/transpile-only")] : []), resolve(__dirname, "apps/round6-boundary.cjs")], { cwd: resolve(__dirname, ".."), env, encoding: "utf8", timeout: 25000 });
  if (result.status !== 0) throw new Error(`${mode}/${source}/${limit}: ${result.error ?? ""}\n${result.stderr}\n${result.stdout}`);
  return JSON.parse(result.stdout.trim());
}
test.each([false, true].flatMap(source => [16, 128].map(limit => ({ source, limit }))))("A7 real OpenAI completion retains canonical usage at $limit ($source source)", ({ source, limit }) => {
  expect(child("library-limit", source, limit)).toMatchObject({ output: "answer", input: 101, total: 108 });
});
test.each([false, true])("B2 wire secret is sanitized before host SDK value truncation (%s source)", source => {
  expect(child("wire-value-limit", source)).toMatchObject({ protected: true, value: "<redacted>" });
});
test.each([false, true])("A11 neutral Genkit mapping receives agent processor without FI credentials (%s source)", source => {
  expect(child("neutral-genkit", source)).toEqual({ active: true, status: "instrumented", canonical: true });
});
test.each([false, true])("B3 Vercel enable result uses selected provider rather than unrelated global (%s source)", source => {
  expect(child("vercel-provider", source)).toMatchObject({ result: "answer", globalSpans: 0 });
});
test("NDJSON EOF accepts only a complete final JSON record and preserves usage/output/timing", () => {
  const record = JSON.stringify({ model: "fixture-model", response: "hello", done: true, prompt_eval_count: 11, eval_count: 4 });
  const url = "http://127.0.0.1:11434/api/generate", found = match("POST", url)!;
  const results = [record, record + "\n"].map(raw => {
    const body = Buffer.from(raw), arrivals = [{ end: body.length, seconds: 0.03 }];
    expect(streamFrames(body, false)).toHaveLength(1);
    expect(streamingMilestones(body, false, arrivals)).toEqual({ events: [0.03], firstText: 0.03 });
    return build(found, { method: "POST", url, headers: {}, body: Buffer.from('{"model":"fixture-model","prompt":"hello","stream":true}') }, { status: 200, headers: { "content-type": "application/x-ndjson" }, body, size: body.length, arrivalTimes: arrivals, firstChunkSeconds: 0.03 })!.attributes;
  });
  for (const attrs of results) expect(attrs).toMatchObject({ "output.value": "hello", "gen_ai.usage.input_tokens": 11, "gen_ai.usage.output_tokens": 4, "gen_ai.response.time_to_first_text": 0.03 });
  expect(streamFrames(Buffer.from(record.slice(0, -1)), false)).toEqual([]);
  expect(streamFrames(Buffer.from(record), false, Infinity, false)).toEqual([]);
  expect(streamFrames(Buffer.from('data: {"response":"hello"}\n'), true)).toEqual([]);
});
