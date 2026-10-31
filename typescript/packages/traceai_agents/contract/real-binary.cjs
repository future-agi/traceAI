#!/usr/bin/env node
"use strict";
// Opt-in real-binary isolation smoke. No user homes, auth discovery, login or remote network.
// This checks the upstream binaries against local synthetic models, NOT adapter qualification.
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { spawn } = require("node:child_process");
async function run({ binary, kind, root, sandbox }) {
  if (
    process.platform !== "darwin" ||
    !path.isAbsolute(root) ||
    !path.isAbsolute(binary) ||
    !path.isAbsolute(sandbox)
  )
    throw Error("ISOLATED_MAC_RUN_REQUIRED");
  fs.mkdirSync(root, { recursive: true, mode: 0o700 });
  const home = fs.mkdtempSync(path.join(root, kind + "-"));
  fs.mkdirSync(path.join(home, "agent"), { mode: 0o700 });
  let calls = 0;
  let bytes = 0;
  const server = http.createServer(async (req, res) => {
    let size = 0;
    const chunks = [];
    for await (const chunk of req) {
      size += chunk.length;
      if (size > 2 * 1024 * 1024) {
        res.writeHead(413).end();
        return;
      }
      chunks.push(chunk);
    }
    bytes += size;
    if (req.url?.includes("count_tokens")) {
      res.setHeader("content-type", "application/json");
      res.end('{"input_tokens":1}');
      return;
    }
    if (!req.url?.includes("/responses") && !req.url?.includes("/messages")) {
      res.writeHead(404).end();
      return;
    }
    calls++;
    const body = JSON.parse(Buffer.concat(chunks).toString());
    if (req.url.includes("/responses")) {
      const item = {
        id: "msg_fixture",
        type: "message",
        role: "assistant",
        status: "completed",
        content: [{ type: "output_text", text: "fixture-ok", annotations: [] }],
      };
      const response = {
        id: "resp_fixture",
        object: "response",
        created_at: 1,
        status: "completed",
        model: "fixture-model",
        output: [item],
        usage: {
          input_tokens: 10,
          output_tokens: 2,
          total_tokens: 12,
          input_tokens_details: { cached_tokens: 0 },
          output_tokens_details: { reasoning_tokens: 0 },
        },
      };
      if (body.stream) {
        res.writeHead(200, { "content-type": "text/event-stream" });
        let sequence = 0;
        for (const event of [
          {
            type: "response.created",
            response: { ...response, status: "in_progress", output: [] },
          },
          {
            type: "response.output_item.added",
            output_index: 0,
            item: { ...item, status: "in_progress", content: [] },
          },
          {
            type: "response.content_part.added",
            item_id: item.id,
            output_index: 0,
            content_index: 0,
            part: { type: "output_text", text: "", annotations: [] },
          },
          {
            type: "response.output_text.delta",
            item_id: item.id,
            output_index: 0,
            content_index: 0,
            delta: "fixture-ok",
          },
          {
            type: "response.output_text.done",
            item_id: item.id,
            output_index: 0,
            content_index: 0,
            text: "fixture-ok",
          },
          {
            type: "response.content_part.done",
            item_id: item.id,
            output_index: 0,
            content_index: 0,
            part: item.content[0],
          },
          { type: "response.output_item.done", output_index: 0, item },
          { type: "response.completed", response },
        ])
          res.write(
            "event: " +
              event.type +
              "\ndata: " +
              JSON.stringify({ ...event, sequence_number: sequence++ }) +
              "\n\n",
          );
        res.end();
      } else {
        res.setHeader("content-type", "application/json");
        res.end(JSON.stringify(response));
      }
    } else {
      const message = {
        id: "msg_fixture",
        type: "message",
        role: "assistant",
        model: body.model,
        content: [{ type: "text", text: "fixture-ok" }],
        stop_reason: "end_turn",
        stop_sequence: null,
        usage: { input_tokens: 10, output_tokens: 2 },
      };
      if (body.stream) {
        res.writeHead(200, { "content-type": "text/event-stream" });
        for (const event of [
          {
            type: "message_start",
            message: {
              ...message,
              content: [],
              stop_reason: null,
              usage: { input_tokens: 10, output_tokens: 0 },
            },
          },
          {
            type: "content_block_start",
            index: 0,
            content_block: { type: "text", text: "" },
          },
          {
            type: "content_block_delta",
            index: 0,
            delta: { type: "text_delta", text: "fixture-ok" },
          },
          { type: "content_block_stop", index: 0 },
          {
            type: "message_delta",
            delta: { stop_reason: "end_turn", stop_sequence: null },
            usage: { output_tokens: 2 },
          },
          { type: "message_stop" },
        ])
          res.write(
            "event: " +
              event.type +
              "\ndata: " +
              JSON.stringify(event) +
              "\n\n",
          );
        res.end();
      } else {
        res.setHeader("content-type", "application/json");
        res.end(JSON.stringify(message));
      }
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const origin = "http://127.0.0.1:" + server.address().port;
  const env = {
    PATH: process.env.PATH,
    HOME: home,
    TMPDIR: home,
    CODEX_HOME: path.join(home, "agent"),
    CLAUDE_CONFIG_DIR: path.join(home, "agent"),
    ANTHROPIC_BASE_URL: origin,
    ANTHROPIC_API_KEY: "synthetic-fixture-key",
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
    DISABLE_AUTOUPDATER: "1",
    NO_PROXY: "127.0.0.1,localhost",
  };
  const args =
    kind === "codex"
      ? [
          "exec",
          "--skip-git-repo-check",
          "--sandbox",
          "read-only",
          "--json",
          "-c",
          'model_provider="fixture"',
          "-c",
          'model_providers.fixture.name="Fixture"',
          "-c",
          `model_providers.fixture.base_url="${origin}/v1"`,
          "-c",
          'model_providers.fixture.wire_api="responses"',
          "-c",
          "model_providers.fixture.requires_openai_auth=false",
          "-c",
          'model="fixture-model"',
          "Say fixture-ok only.",
        ]
      : [
          "--print",
          "--model",
          "claude-sonnet-4-6",
          "--output-format",
          "json",
          "Say fixture-ok only.",
        ];
  let stdout = "",
    outputBytes = 0,
    timedOut = false;
  try {
    const result = await new Promise((resolve, reject) => {
      const child = spawn(
        "/usr/bin/sandbox-exec",
        ["-f", sandbox, binary, ...args],
        { cwd: home, env, stdio: ["ignore", "pipe", "pipe"], detached: true },
      );
      const kill = () => {
        try {
          process.kill(-child.pid, "SIGKILL");
        } catch {
          child.kill("SIGKILL");
        }
      };
      const timer = setTimeout(() => {
        timedOut = true;
        kill();
      }, 60000);
      child.stdout.on("data", (chunk) => {
        outputBytes += chunk.length;
        if (outputBytes > 2 * 1024 * 1024) kill();
        else stdout += chunk.toString();
      });
      child.stderr.on("data", (chunk) => {
        outputBytes += chunk.length;
        if (outputBytes > 2 * 1024 * 1024) kill();
      });
      child.on("error", () => {
        clearTimeout(timer);
        reject(Error("BINARY_START_FAILED"));
      });
      child.on("exit", (code, signal) => {
        clearTimeout(timer);
        resolve({ code, signal });
      });
    });
    return {
      kind,
      ...result,
      timedOut,
      modelRequests: calls,
      requestBytes: bytes,
      fixtureOutput: stdout.includes("fixture-ok"),
      scope: "isolated-real-binary-model-transport-only",
      adapterQualification: "NOT_ESTABLISHED",
    };
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
}
module.exports = { run };
if (require.main === module) {
  const [binary, kind, root, sandbox] = process.argv.slice(2);
  run({ binary, kind, root, sandbox }).then(
    (result) => {
      console.log(JSON.stringify(result));
      process.exitCode =
        result.code === 0 && result.modelRequests > 0 && result.fixtureOutput
          ? 0
          : 1;
    },
    () => {
      console.log(
        JSON.stringify({ state: "failed", code: "ISOLATION_SMOKE_FAILED" }),
      );
      process.exitCode = 1;
    },
  );
}
