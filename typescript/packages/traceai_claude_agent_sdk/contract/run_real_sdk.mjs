// Contract script: the REAL @anthropic-ai/claude-agent-sdk query() (and its
// bundled Claude Code CLI) against a loopback mock of the Anthropic Messages
// API, reached through options.env.ANTHROPIC_BASE_URL. No Anthropic call:
// the API key is a placeholder, HTTPS_PROXY/HTTP_PROXY point at a dead
// loopback port, and HOME / CLAUDE_CONFIG_DIR are a fresh temp dir.
//
// The mock answers the first /v1/messages call with a Read tool_use for a temp
// file and the second with text, so the CLI runs one real tool ("tool" and
// "close" scenarios). Other scenarios get a text reply to every call.
//
// Env: FI_BASE_URL, FI_API_KEY, FI_SECRET_KEY, FI_PROJECT_NAME (fi-core).
//      CLAUDE_AGENT_SDK_ENTRY (optional): absolute path to another SDK build's
//      sdk.mjs, to run the same contract against an older 0.3.x.
//      SCENARIO (optional, default "tool"):
//        tool       one query() that runs the Read tool.
//        close      two query() calls stopped mid-tool: one with close(), one
//                   with Symbol.asyncDispose.
//        resume     two query() calls; the second resumes the first's session.
//        continue_fork  three query() calls: a new session, options.continue,
//                   then resume + forkSession from the first session id.
//        restart    one query() per process, sharing WORKDIR (HOME): PHASE=first
//                   starts a session, PHASE=second resumes RESUME_SESSION_ID.
//        streaming  one query() with an AsyncIterable prompt of two user turns;
//                   the second turn is sent after the first result.
//        streaming_close  one streaming-input query(); after its result the app
//                   calls close() while the prompt iterable is still open (the
//                   usual way to end a streaming-input session).
//      WORKDIR (optional): use and keep this directory instead of a temp dir.
// Prints {"requests": [...], "messages": [...], "queries": [[...], ...]} on stdout.
import http from "node:http";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { ProjectType, register } from "@traceai/fi-core";
import { shutdown, wrapQuery } from "@traceai/claude-agent-sdk";

const { query } = process.env.CLAUDE_AGENT_SDK_ENTRY
  ? await import(process.env.CLAUDE_AGENT_SDK_ENTRY)
  : await import("@anthropic-ai/claude-agent-sdk");

const MOCK_MODEL = "claude-sonnet-4-5";
const PROMPT = "Read README.md and summarize it. SECRET_PROMPT_MARKER";
const SCENARIO = process.env.SCENARIO || "tool";
const TOOL_FLOW = SCENARIO === "tool" || SCENARIO === "close";

const keepWorkdir = Boolean(process.env.WORKDIR);
const workdir = keepWorkdir ? process.env.WORKDIR : fs.mkdtempSync(path.join(os.tmpdir(), "th8235-real-sdk-"));
fs.mkdirSync(workdir, { recursive: true });
const readmePath = path.join(workdir, "README.md");
fs.writeFileSync(readmePath, "# Demo\nSECRET_TOOL_OUTPUT_MARKER\n");

const requests = [];
function sendSse(res, model, blocks, stopReason) {
  res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", "request-id": "req_mock" });
  const send = (event, data) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  send("message_start", {
    type: "message_start",
    message: {
      id: `msg_mock_${requests.length}`, type: "message", role: "assistant", model, content: [],
      stop_reason: null, stop_sequence: null,
      usage: { input_tokens: 20, output_tokens: 1, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 },
    },
  });
  blocks.forEach((block, index) => {
    if (block.type === "text") {
      send("content_block_start", { type: "content_block_start", index, content_block: { type: "text", text: "" } });
      send("content_block_delta", { type: "content_block_delta", index, delta: { type: "text_delta", text: block.text } });
    } else {
      send("content_block_start", { type: "content_block_start", index, content_block: { type: "tool_use", id: block.id, name: block.name, input: {} } });
      send("content_block_delta", { type: "content_block_delta", index, delta: { type: "input_json_delta", partial_json: JSON.stringify(block.input) } });
    }
    send("content_block_stop", { type: "content_block_stop", index });
  });
  send("message_delta", { type: "message_delta", delta: { stop_reason: stopReason, stop_sequence: null }, usage: { output_tokens: 8 } });
  send("message_stop", { type: "message_stop" });
  res.end();
}

const server = http.createServer((req, res) => {
  let body = "";
  req.on("data", (chunk) => (body += chunk));
  req.on("end", () => {
    let parsed = {};
    try { parsed = JSON.parse(body || "{}"); } catch { /* not JSON */ }
    const hasToolResult = JSON.stringify(parsed.messages ?? []).includes('"tool_result"');
    requests.push({
      method: req.method,
      url: req.url,
      stream: parsed.stream === true,
      model: parsed.model,
      xApiKey: req.headers["x-api-key"] ?? null,
      hasToolResult,
    });
    if (req.method === "POST" && req.url.startsWith("/v1/messages") && !req.url.includes("count_tokens")) {
      const model = parsed.model || MOCK_MODEL;
      if (TOOL_FLOW && !hasToolResult) {
        return sendSse(res, model, [
          { type: "text", text: "Reading it. SECRET_ASSISTANT_TEXT_MARKER" },
          { type: "tool_use", id: "toolu_mock_read", name: "Read", input: { file_path: readmePath } },
        ], "tool_use");
      }
      return sendSse(res, model, [{ type: "text", text: "It is a demo README." }], "end_turn");
    }
    res.writeHead(404, { "content-type": "application/json" });
    res.end(JSON.stringify({ type: "error", error: { type: "not_found_error", message: "mock" } }));
  });
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const mockOrigin = `http://127.0.0.1:${server.address().port}`;

const provider = register({ projectType: ProjectType.OBSERVE });
const tracedQuery = wrapQuery(query, { tracerProvider: provider });

const abortController = new AbortController();
const timer = setTimeout(() => abortController.abort(), 90_000);

function options(extra = {}) {
  return {
    model: MOCK_MODEL,
    cwd: workdir,
    maxTurns: 3,
    allowedTools: ["Read"],
    permissionMode: "default",
    settingSources: [],
    persistSession: false,
    abortController,
    // Options.env REPLACES the subprocess environment: pass everything the CLI needs.
    env: {
      PATH: process.env.PATH,
      HOME: workdir,
      CLAUDE_CONFIG_DIR: path.join(workdir, ".claude"),
      ANTHROPIC_BASE_URL: mockOrigin,
      ANTHROPIC_API_KEY: "sk-ant-PLACEHOLDER-not-a-key",
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
      DISABLE_TELEMETRY: "1",
      DISABLE_ERROR_REPORTING: "1",
      DISABLE_AUTOUPDATER: "1",
      HTTPS_PROXY: "http://127.0.0.1:9",
      HTTP_PROXY: "http://127.0.0.1:9",
      NO_PROXY: "127.0.0.1,localhost",
    },
    ...extra,
  };
}

async function drain(stream) {
  const out = [];
  for await (const message of stream) out.push(message);
  return out;
}

/** Read until the assistant asks for the Read tool (conversation, turn and tool spans open). */
async function readUntilToolUse(stream) {
  const out = [];
  for (;;) {
    const { value, done } = await stream.next();
    if (done) return out;
    out.push(value);
    const content = value.type === "assistant" ? value.message?.content ?? [] : [];
    if (content.some((block) => block.type === "tool_use")) return out;
  }
}

const SCENARIOS = {
  async tool() {
    return [await drain(tracedQuery({ prompt: PROMPT, options: options() }))];
  },
  async close() {
    const closed = tracedQuery({ prompt: PROMPT, options: options() });
    const first = await readUntilToolUse(closed);
    closed.close();
    const disposed = tracedQuery({ prompt: PROMPT, options: options() });
    const second = await readUntilToolUse(disposed);
    await disposed[Symbol.asyncDispose]();
    return [first, second];
  },
  async resume() {
    const first = await drain(tracedQuery({ prompt: "First question.", options: options({ persistSession: true }) }));
    const second = await drain(
      tracedQuery({ prompt: "Second question.", options: options({ persistSession: true, resume: sessionOf(first) }) }),
    );
    return [first, second];
  },
  async continue_fork() {
    const first = await drain(tracedQuery({ prompt: "First question.", options: options({ persistSession: true }) }));
    const second = await drain(
      tracedQuery({ prompt: "Second question.", options: options({ persistSession: true, continue: true }) }),
    );
    const third = await drain(
      tracedQuery({
        prompt: "Third question.",
        options: options({ persistSession: true, resume: sessionOf(first), forkSession: true }),
      }),
    );
    return [first, second, third];
  },
  async restart() {
    const resume = process.env.PHASE === "second" ? process.env.RESUME_SESSION_ID : undefined;
    if (process.env.PHASE === "second" && !resume) throw new Error("PHASE=second needs RESUME_SESSION_ID");
    return [
      await drain(
        tracedQuery({
          prompt: resume ? "Second question." : "First question.",
          options: options({ persistSession: true, ...(resume ? { resume } : {}) }),
        }),
      ),
    ];
  },
  async streaming() {
    let resultSeen;
    let waitForResult = new Promise((resolve) => (resultSeen = resolve));
    const userTurn = (content) => ({ type: "user", message: { role: "user", content }, parent_tool_use_id: null });
    async function* prompt() {
      yield userTurn("First question.");
      await waitForResult;
      waitForResult = new Promise((resolve) => (resultSeen = resolve));
      yield userTurn("Second question.");
      await waitForResult;
    }
    const out = [];
    for await (const message of tracedQuery({ prompt: prompt(), options: options() })) {
      out.push(message);
      if (message.type === "result") resultSeen();
    }
    return [out];
  },
  async streaming_close() {
    let release;
    const held = new Promise((resolve) => (release = resolve));
    const userTurn = (content) => ({ type: "user", message: { role: "user", content }, parent_tool_use_id: null });
    async function* prompt() {
      yield userTurn("First question.");
      await held; // more input could still come: the stream stays open
    }
    const q = tracedQuery({ prompt: prompt(), options: options() });
    const out = [];
    for (;;) {
      const { value, done } = await q.next();
      if (done) break;
      out.push(value);
      if (value.type === "result") break;
    }
    q.close();
    release();
    return [out];
  },
};

function sessionOf(messages) {
  const init = messages.find((m) => m.type === "system" && m.subtype === "init");
  if (!init) throw new Error("no init message");
  return init.session_id;
}

const queries = [];
try {
  const scenario = SCENARIOS[SCENARIO];
  if (!scenario) throw new Error(`unknown SCENARIO ${SCENARIO}`);
  queries.push(...(await scenario()));
} finally {
  clearTimeout(timer);
  await shutdown(provider);
  await provider.shutdown();
  server.close();
  if (!keepWorkdir) fs.rmSync(workdir, { recursive: true, force: true });
}

process.stdout.write(JSON.stringify({ mockOrigin, requests, messages: queries.flat(), queries }));
