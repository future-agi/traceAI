const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  fixture,
  fs,
  path,
  spans,
  attributes,
  payload,
} = require("./helpers.cjs");
const {
  TranscriptReader,
  correlate,
  CursorObserver,
  normalizeEvent,
} = require("../dist/index.js");
const rows = (text) =>
  [
    {
      role: "user",
      message: {
        content: [{ type: "text", text: `<user_query> ${text} </user_query>` }],
      },
    },
    {
      role: "assistant",
      message: { content: [{ type: "text", text: "answer" }] },
    },
    { type: "turn_ended", status: "success" },
  ]
    .map(JSON.stringify)
    .join("\n") + "\n";
async function file(f, contents) {
  const p = path.join(
    f.home,
    ".cursor/projects/sample/agent-transcripts/session/session.jsonl",
  );
  await fs.mkdir(path.dirname(p), { recursive: true, mode: 0o700 });
  await fs.writeFile(p, contents, { mode: 0o600 });
  return p;
}
test("incremental reads serialize concurrency and preserve partial tail without duplicating message ids", async (t) => {
  const f = await fixture(t),
    full = rows("hello"),
    p = await file(f, full.slice(0, -9)),
    reader = new TranscriptReader(f.config);
  const first = await reader.read(p);
  assert.equal(first.gap, "TRANSCRIPT_PARTIAL_TAIL");
  assert.equal(correlate(first, "hello"), undefined);
  await fs.appendFile(p, full.slice(-9));
  const [a, b] = await Promise.all([reader.read(p), reader.read(p)]);
  assert.equal(a.gap, undefined);
  assert.deepEqual(a, b);
  assert.equal(correlate(a, "hello").messages.length, 1);
  assert.equal(correlate(a, "other"), undefined);
  await fs.appendFile(p, rows("hello"));
  assert.equal(
    correlate(await reader.read(p), "hello"),
    undefined,
    "Repeated prompts must not be guessed",
  );
});
test("truncation, replacement and malformed lines are gaps, not successful message reconstruction", async (t) => {
  const f = await fixture(t),
    p = await file(f, rows("long enough prompt ".repeat(10))),
    reader = new TranscriptReader(f.config);
  await reader.read(p);
  await fs.writeFile(p, rows("new"));
  assert.equal((await reader.read(p)).gap, "TRANSCRIPT_TRUNCATED");
  const malformed = await file(f, "{broken}\n" + rows("hi"));
  const result = await new TranscriptReader(f.config).read(malformed);
  assert.equal(result.gap, "TRANSCRIPT_MALFORMED_LINE");
  assert.equal(correlate(result, "hi"), undefined);
});
test("arbitrary files, symlinks and capture-disabled paths are never read", async (t) => {
  const f = await fixture(t),
    p = await file(f, rows("hi"));
  const outside = path.join(f.home, "outside.jsonl");
  await fs.writeFile(outside, rows("secret"));
  const reader = new TranscriptReader(f.config);
  assert.equal((await reader.read(outside)).gap, "TRANSCRIPT_PATH_REJECTED");
  await fs.unlink(p);
  await fs.symlink(outside, p);
  assert.equal((await reader.read(p)).gap, "TRANSCRIPT_UNAVAILABLE");
  assert.equal(
    (await new TranscriptReader({ ...f.config, captureContent: false }).read(p))
      .gap,
    "CONTENT_DISABLED",
  );
});
test("final turn closes at stop; reconstructed messages are labeled, with no per-request invented usage", async (t) => {
  const f = await fixture(t),
    p = await file(f, rows("hello")),
    observer = new CursorObserver(f.config);
  for (const [event, extra] of [
    ["beforeSubmitPrompt", { prompt: "hello", transcript_path: p }],
    [
      "stop",
      {
        status: "completed",
        transcript_path: p,
        input_tokens: 10,
        output_tokens: 5,
      },
    ],
  ])
    await observer.accept(
      normalizeEvent(payload(event, extra), event, f.config),
    );
  await observer.close();
  const all = spans(f.requests);
  assert.equal(all.length, 2);
  const message = all.find((s) => s.name === "assistant-message");
  const attr = attributes(message);
  assert.equal(attr["fi.agent.fidelity"], "reconstructed-message");
  assert.equal(attr["fi.span.kind"], "LLM");
  assert.equal(attr["fi.agent.timing"], "reconstructed");
  assert.equal(Number(attr["gen_ai.usage.input_tokens"]), 10);
  assert.equal(Number(attr["gen_ai.usage.output_tokens"]), 5);
  assert.equal(attr["fi.agent.usage.additive"], true);
  assert.equal(attr["output.value"], "answer");
});
