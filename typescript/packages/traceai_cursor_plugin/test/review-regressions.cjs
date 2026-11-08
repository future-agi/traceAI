// Shared by the package tests and fresh-tarball replay. Only sanitized hooks and synthetic content.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const { load, attrs, prepare } = require("./round2-golden.cjs");
const terminalCases = [
  "print-terminal-only",
  "print-terminal-no-generation",
  "response-stop-no-prompt",
  "stop-no-prompt",
  "response-session-end-no-prompt",
];
async function terminalRegression({ send, readSpans }, name, capture = true) {
  const print = name.startsWith("print-");
  const wanted = print
    ? ["sessionEnd"]
    : name === "stop-no-prompt"
      ? ["stop", "sessionEnd"]
      : name === "response-session-end-no-prompt"
        ? ["afterAgentResponse", "sessionEnd"]
        : ["afterAgentResponse", "stop", "sessionEnd"];
  const conversation = `review-${name}`;
  const rows = load(print ? "cli-print" : "cli2")
    .filter((r) => wanted.includes(r.hook_event_name))
    .map((r) => ({
      ...r,
      conversation_id: conversation,
      generation_id:
        name === "print-terminal-no-generation"
          ? undefined
          : r.hook_event_name === "sessionEnd"
            ? conversation
            : `${conversation}-turn`,
      transcript_path: null,
    }));
  const before = readSpans().length;
  // Exact repeats, a sessionEnd whose generation_id differs from stop, and a full replay
  // all use separate hook processes: in-memory deduplication alone cannot pass this.
  for (let pass = 0; pass < 2; pass++)
    for (const row of rows) {
      await send(row);
      await send(row);
    }
  const all = readSpans().slice(before);
  if (print) {
    assert.equal(all.length, 0, `${name}: terminal alone must not invent a turn`);
    return { case: name, spanCount: 0, duplicateTerminalsSuppressed: true, tree: [] };
  }
  assert.equal(
    all.length,
    1,
    `${name}: one root, no invented tools or model calls`,
  );
  const root = all[0],
    a = attrs(root);
  assert.ok(!root.parentSpanId);
  assert.equal(a["fi.span.kind"], "AGENT");
  assert.equal(a["input.value"], undefined);
  // cli2's sessionEnd reports aborted; a preceding stop reports completed and wins.
  assert.equal(
    a["fi.agent.outcome"],
    name === "response-session-end-no-prompt" ? "interrupted" : "success",
  );
  const gap =
    print || name === "response-session-end-no-prompt"
      ? "MISSING_PROMPT_AND_STOP"
      : "MISSING_PROMPT";
  assert.equal(a["fi.agent.gap"], gap);
  if (print) {
    assert.equal(a["output.value"], undefined);
    assert.ok(
      Object.keys(a).every(
        (key) =>
          !key.startsWith("gen_ai.usage.") &&
          !key.startsWith("fi.agent.summary."),
      ),
    );
  } else {
    assert.equal(a["fi.usage.contribution"], "additive");
    assert.equal(a["fi.agent.usage.additive"], true);
    assert.equal(a["fi.usage.scope"], "turn");
    for (const [key, value] of Object.entries({
      input_tokens: 78759,
      output_tokens: 329,
      cache_read_tokens: 67584,
      total_tokens: 79088,
    }))
      assert.equal(Number(a[`gen_ai.usage.${key}`]), value);
    if (capture && wanted.includes("afterAgentResponse"))
      assert.equal(a["output.value"], rows.find((r) => r.text).text);
    else assert.equal(a["output.value"], undefined);
  }
  return {
    case: name,
    spanCount: all.length,
    duplicateTerminalsSuppressed: true,
    tree: [
      `cursor [AGENT] gap=${gap}${print ? " no-prompt no-usage" : " tokens=78759/329 cache-read=67584 additive"}`,
    ],
  };
}
async function structuredRedactionRegression({ home, send, readSpans }) {
  const transcript = await prepare(home, "cli2");
  const sentinels = [];
  const credentials = (where) =>
    Object.fromEntries(
      [
        "password",
        "token",
        "secret",
        "API-key",
        "accessToken",
        "client_secret",
        "private_key",
        "secretkey",
        "aws_secret_access_key",
        "clientSecretKey",
        "SECRET-KEY-VALUE",
        "token_value",
        "password_hash",
        "passwd_value",
        "api_key_value",
        "privateKeyPem",
        "access_key_id",
        "credentials",
        "authorization_header",
        "cookie_value",
        "session_token_value",
      ].map((key) => {
        const value = `SYNTHETIC_VALUE_${where}_${sentinels.length}`;
        sentinels.push(value);
        return [key, value];
      }),
    );
  const rows = [
    { role: "user", message: { content: "safe fixture prompt" } },
    {
      role: "assistant",
      message: {
        content: [
          { type: "text", text: "Calling the tool." },
          {
            type: "tool_use",
            id: "fixture-call",
            name: "MCP",
            input: {
              visible: "safe-arguments",
              nested: [credentials("arguments")],
              encoded: JSON.stringify(credentials("encoded-arguments")),
              doubleEncoded: JSON.stringify(
                JSON.stringify(credentials("double-encoded-arguments")),
              ),
            },
          },
        ],
      },
    },
    {
      role: "user",
      message: {
        content: [
          {
            type: "tool_result",
            tool_use_id: "fixture-call",
            content: {
              visible: "safe-results",
              nested: [credentials("results")],
              encoded: JSON.stringify(credentials("encoded-results")),
              doubleEncoded: JSON.stringify(
                JSON.stringify(credentials("double-encoded-results")),
              ),
            },
          },
        ],
      },
    },
    { role: "assistant", message: { content: "finished" } },
    { type: "turn_ended", status: "success" },
  ];
  await fs.writeFile(
    transcript.file,
    rows.map(JSON.stringify).join("\n") + "\n",
  );
  const before = readSpans().length;
  // There are deliberately NO tool hooks: the transcript export must be safe on its own.
  for (const row of load("cli2").filter((r) =>
    ["beforeSubmitPrompt", "afterAgentResponse", "stop", "sessionEnd"].includes(
      r.hook_event_name,
    ),
  )) {
    await send({
      ...row,
      conversation_id: "review-structured-redaction",
      generation_id:
        row.hook_event_name === "sessionEnd"
          ? "review-structured-redaction"
          : "review-redaction-turn",
      ...(row.hook_event_name === "beforeSubmitPrompt"
        ? { prompt: "safe fixture prompt" }
        : {}),
      ...(row.hook_event_name === "afterAgentResponse"
        ? { text: "finished" }
        : {}),
      transcript_path: transcript.file,
    });
  }
  const all = readSpans().slice(before);
  const generations = all
    .filter((s) => attrs(s)["fi.span.kind"] === "LLM")
    .sort((a, b) =>
      Number(BigInt(a.startTimeUnixNano) - BigInt(b.startTimeUnixNano)),
    );
  assert.equal(all.length, 3);
  assert.equal(generations.length, 2);
  const first = attrs(generations[0]),
    last = attrs(generations[1]);
  assert.match(first["output.value"], /tool_calls.*MCP.*safe-arguments/s);
  assert.match(first["output.value"], /\[REDACTED\]|<redacted>/);
  assert.match(last["input.value"], /safe-arguments/);
  assert.match(last["input.value"], /safe-results/);
  assert.match(last["input.value"], /\[REDACTED\]|<redacted>/);
  const serialized = JSON.stringify(all);
  for (const sentinel of sentinels)
    assert.ok(
      !serialized.includes(sentinel),
      "a structured credential reached exported OTLP",
    );
  return {
    case: "structured-credential-redaction",
    generationCount: generations.length,
    syntheticCredentialsChecked: sentinels.length,
    leakingOutputs: 0,
    leakingInputs: 0,
    tree: [
      "cursor [AGENT]",
      "  assistant-message #1 [LLM] redacted-tool-calls",
      "  assistant-message #2 [LLM] redacted-context tokens=78759/329 cache-read=67584",
    ],
  };
}
module.exports = {
  terminalCases,
  terminalRegression,
  structuredRedactionRegression,
};
