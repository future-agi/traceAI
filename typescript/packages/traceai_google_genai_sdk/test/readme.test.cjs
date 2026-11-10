const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const http = require("node:http");
const { execFile } = require("node:child_process");
const { promisify } = require("node:util");
const exec = promisify(execFile);
const { modern } = require("./interaction-fixtures.cjs");

test("exact README ESM snippet uses published FI core and exports real authenticated OTLP", async () => {
  const consumer = process.env.CONSUMER_DIR;
  assert.ok(consumer, "Run this test with a clean packed CONSUMER_DIR");
  const readme = await fs.readFile(
    path.join(__dirname, "../README.md"),
    "utf8",
  );
  const code = readme.match(/```javascript\n([\s\S]+?)\n```/)[1];
  const script = path.join(consumer, "example.mjs");
  await fs.writeFile(script, code);
  const messages = [];
  const modelHeaders = [];
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = Buffer.concat(chunks).toString();
    if (req.url === "/tracer/v1/traces") {
      messages.push({ headers: req.headers, body: JSON.parse(body) });
      res.setHeader("content-type", "application/json");
      return res.end("{}");
    }
    modelHeaders.push(req.headers);
    const response = {
      candidates: [
        {
          content: { role: "model", parts: [{ text: "Book Nook" }] },
          finishReason: "STOP",
        },
      ],
      usageMetadata: {
        promptTokenCount: 4,
        candidatesTokenCount: 2,
        totalTokenCount: 6,
      },
    };
    if (req.url.includes("streamGenerateContent")) {
      res.setHeader("content-type", "text/event-stream");
      return res.end(`data: ${JSON.stringify(response)}\n\n`);
    }
    res.setHeader("content-type", "application/json");
    res.end(JSON.stringify(response));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const baseUrl = `http://127.0.0.1:${server.address().port}`;
  try {
    const { stdout } = await exec(process.execPath, [script], {
      env: {
        PATH: process.env.PATH,
        GOOGLE_API_KEY: "synthetic-google-key",
        FI_API_KEY: "synthetic-fi-key",
        FI_SECRET_KEY: "synthetic-fi-secret",
        FI_BASE_URL: baseUrl,
        GOOGLE_GEMINI_BASE_URL: baseUrl,
      },
    });
    assert.match(stdout, /Book Nook/);
    assert.equal(modelHeaders.length, 2);
    assert.ok(
      modelHeaders.every(
        (h) => h["x-secret-key"] === undefined && h["x-api-key"] === undefined,
      ),
    );
    assert.ok(messages.length);
    assert.ok(
      messages.every(
        (m) =>
          m.headers["x-api-key"] === "synthetic-fi-key" &&
          m.headers["x-secret-key"] === "synthetic-fi-secret",
      ),
    );
    const wire = JSON.stringify(messages.map((m) => m.body));
    assert.doesNotMatch(
      wire,
      /synthetic-fi-key|synthetic-fi-secret|synthetic-google-key/,
    );
    const spans = messages.flatMap((m) =>
      m.body.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((s) => s.spans)),
    );
    assert.equal(spans.length, 2);
    assert.ok(spans.every((s) => s.endTimeUnixNano));
    let types = `import { GoogleGenAI } from '@google/genai';\nimport { instrumentGoogleGenAI, type GoogleGenAITracingOptions, VERSION } from '@traceai/google-genai-sdk';\nconst options: GoogleGenAITracingOptions = {traceConfig: {hideInputs: true}};\nconst h = instrumentGoogleGenAI(new GoogleGenAI({apiKey: 'synthetic'}), options); h.disable(); h.enable(); h.dispose(); const version: string = VERSION;\n`;
    if (modern)
      types += `
import type { Interactions } from '@google/genai';
const step: Interactions.ModelOutputStep = {type: 'model_output', content: [{type: 'text', text: 'CURRENT_OUTPUT_SENTINEL'}]};
const delta: Interactions.StepDelta = {event_type: 'step.delta', index: 0, delta: {type: 'text', text: 'same'}};
const usage: Interactions.Usage = {total_input_tokens: 10, total_output_tokens: 3, total_thought_tokens: 7, total_tokens: 20};
const error: Interactions.ErrorEvent = {event_type: 'error', error: {code: 'INTERNAL', message: 'PRIVATE_ERROR_SENTINEL'}};
const params: Parameters<GoogleGenAI['interactions']['create']>[0] = {model: 'gemini-test', input: 'hello', system_instruction: 'SYSTEM_SENTINEL'};
`;
    await fs.writeFile(path.join(consumer, "types.mts"), types);
    await exec(process.execPath, [
      path.join(consumer, "node_modules/typescript/bin/tsc"),
      "--noEmit",
      "--skipLibCheck",
      "--module",
      "Node16",
      "--moduleResolution",
      "Node16",
      "--target",
      "ES2022",
      path.join(consumer, "types.mts"),
    ]);
    const cjs = await exec(
      process.execPath,
      [
        "-e",
        `const p=require('@traceai/google-genai-sdk');const manifest=require(require('path').resolve(require.resolve('@traceai/google-genai-sdk'),'../../package.json'));if(p.VERSION!==manifest.version||typeof p.instrumentGoogleGenAI!=='function')process.exit(1);`,
      ],
      { cwd: consumer },
    );
    assert.equal(cjs.stderr, "");
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});
