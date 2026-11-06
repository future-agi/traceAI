// Installed-hook regressions, reused against freshly packed artifacts in E2B.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { attrs } = require("./round2-golden.cjs");
function event(conversation, name, extra = {}) {
  return {
    cursor_version: "3.26.13",
    conversation_id: conversation,
    generation_id: `${conversation}-turn`,
    hook_event_name: name,
    ...extra,
  };
}
const failurePayloads = [
  { success: false },
  { isError: true },
  { exitCode: 7 },
  { is_error: true },
];
async function errorContextRegression({ home, send, readSpans }, hide) {
  const conversation = `round3-error-context-${hide}`;
  const file = path.join(
    home,
    ".cursor/projects/round3/agent-transcripts",
    `${conversation}.jsonl`,
  );
  await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  const markers = [];
  const failed = (flags) => {
    const stderr = `SYNTHETIC_PRIVATE_FAILURE_${markers.length}`;
    markers.push(stderr);
    return { ...flags, stderr };
  };
  const rows = [
    { role: "user", message: { content: "safe error privacy prompt" } },
    {
      role: "assistant",
      message: {
        content: [
          {
            type: "tool_use",
            name: "Shell",
            input: { command: "synthetic-command" },
          },
        ],
      },
    },
    ...failurePayloads.map((flags, index) => ({
      role: "user",
      message: {
        content: [
          {
            type: "tool_result",
            content:
              index === 0
                ? failed(flags)
                : index === 1
                  ? JSON.stringify(failed(flags))
                  : index === 2
                    ? [{ type: "text", text: JSON.stringify(failed(flags)) }]
                    : JSON.stringify(JSON.stringify(failed(flags))),
          },
        ],
      },
    })),
    {
      role: "tool",
      message: { content: JSON.stringify(failed({ exitCode: 1 })) },
    },
    { role: "tool", is_error: true, message: { content: failed({}).stderr } },
    {
      role: "user",
      message: {
        content: [
          { type: "tool_result", is_error: true, content: failed({}).stderr },
        ],
      },
    },
    {
      role: "tool",
      message: {
        content: JSON.stringify({
          success: true,
          exitCode: 0,
          stdout: "SAFE_SUCCESS_CONTEXT",
        }),
      },
    },
    { role: "assistant", message: { content: "finished safely" } },
    { type: "turn_ended" },
  ];
  await fs.writeFile(file, rows.map(JSON.stringify).join("\n") + "\n", {
    mode: 0o600,
  });
  const before = readSpans().length;
  for (const [name, data] of [
    ["beforeSubmitPrompt", { prompt: "safe error privacy prompt" }],
    ["afterAgentResponse", { text: "finished safely" }],
    ["stop", { status: "completed", input_tokens: 100, output_tokens: 10 }],
  ])
    await send(event(conversation, name, { ...data, transcript_path: file }), {
      FI_HIDE_ERROR_DETAILS: String(hide),
    });
  const all = readSpans().slice(before);
  const generations = all.filter((s) => attrs(s)["fi.span.kind"] === "LLM");
  assert.equal(all.length, 3);
  assert.equal(generations.length, 2);
  const last = generations.find(
    (s) => attrs(s)["output.value"] === "finished safely",
  );
  assert.ok(last, "final generation must be exported");
  assert.match(attrs(last)["input.value"], /SAFE_SUCCESS_CONTEXT/);
  for (const marker of markers) {
    assert.equal(
      JSON.stringify(all).includes(marker),
      !hide,
      "error-detail policy must reach exported OTLP",
    );
    assert.equal(attrs(last)["input.value"].includes(marker), !hide);
  }
  return {
    case: "tool-result-error-context",
    hideErrorDetails: hide,
    generationCount: 2,
    failureShapesChecked: markers.length,
    leakingInputs: hide ? 0 : undefined,
  };
}
const boundaryCases = [
  {
    count: 127,
    terminal: "stop",
    status: "completed",
    outcome: "success",
    terminalUsage: true,
  },
  {
    count: 128,
    terminal: "stop",
    status: "error",
    outcome: "error",
    terminalUsage: false,
  },
  {
    count: 132,
    terminal: "sessionEnd",
    status: "aborted",
    outcome: "interrupted",
    terminalUsage: false,
  },
];
async function boundaryRegression(
  { send, readSpans, readDiagnostics },
  scenario,
) {
  const conversation = `round3-boundary-${scenario.count}`;
  const before = readSpans().length;
  await send(
    event(conversation, "beforeSubmitPrompt", {
      prompt: "safe bounded prompt",
    }),
  );
  // 127 detail events: one prompt and 63 completed tool pairs.
  for (let i = 0; i < 63; i++) {
    await send(
      event(conversation, "preToolUse", {
        tool_name: "Read",
        tool_use_id: `call-${i}`,
        tool_input: "safe",
      }),
    );
    await send(
      event(conversation, "postToolUse", {
        tool_name: "Read",
        tool_use_id: `call-${i}`,
        tool_output: "safe",
      }),
    );
  }
  for (let i = 127; i < scenario.count; i++)
    await send(
      event(conversation, "preToolUse", {
        tool_name: "Read",
        tool_use_id: `overflow-${i}`,
        tool_input: "safe",
      }),
    );
  const response = event(conversation, "afterAgentResponse", {
    text: "bounded turn finished",
    input_tokens: 100,
    output_tokens: 10,
  });
  await send(response);
  await send(response);
  // Later response without usage must not erase the previously observed aggregate.
  await send(
    event(conversation, "afterAgentResponse", {
      text: "latest bounded response",
    }),
  );
  const terminal = event(conversation, scenario.terminal, {
    ...(scenario.terminal === "sessionEnd"
      ? { generation_id: conversation, reason: scenario.status }
      : { status: scenario.status }),
    ...(scenario.terminalUsage ? { input_tokens: 200, output_tokens: 20 } : {}),
  });
  await send(terminal);
  const all = readSpans().slice(before);
  const roots = all.filter((s) => !s.parentSpanId);
  assert.equal(roots.length, 1);
  const root = attrs(roots[0]);
  assert.equal(root["fi.agent.outcome"], scenario.outcome);
  assert.equal(root["output.value"], "latest bounded response");
  assert.equal(root["fi.usage.contribution"], "additive");
  assert.equal(all.filter((s) => attrs(s)["fi.span.kind"] === "LLM").length, 0);
  for (const [key, expected] of Object.entries({
    input_tokens: scenario.terminalUsage ? 200 : 100,
    output_tokens: scenario.terminalUsage ? 20 : 10,
    total_tokens: scenario.terminalUsage ? 220 : 110,
  }))
    assert.equal(
      all.reduce((n, s) => n + Number(attrs(s)[`gen_ai.usage.${key}`] ?? 0), 0),
      expected,
    );
  const diagnostics = await readDiagnostics();
  assert.equal(
    diagnostics.gaps.includes("TURN_EVENT_LIMIT"),
    scenario.count > 128,
  );
  assert.ok(!diagnostics.gaps.includes("CURSOR_STATE_REJECTED"));
  await send(terminal);
  await send(
    event(conversation, "sessionEnd", {
      generation_id: conversation,
      reason: "aborted",
    }),
  );
  await send(response);
  assert.equal(
    readSpans().length - before,
    all.length,
    "terminal tombstone must suppress later hooks",
  );
  return {
    case: "bounded-events",
    detailEvents: scenario.count,
    terminal: scenario.terminal,
    outcome: root["fi.agent.outcome"],
    totalTokens: Number(root["gen_ai.usage.total_tokens"]),
    spanCount: all.length,
    duplicateTerminalsSuppressed: true,
  };
}
const fragmentRedactionCases = [
  "json-string-control",
  "two-json-text-blocks",
  "json-with-tool-call",
];
async function fragmentRedactionRegression({ home, send, readSpans }, variant) {
  const conversation = `round3-fragments-${variant}`;
  const file = path.join(
    home,
    ".cursor/projects/round3/agent-transcripts",
    `${conversation}.jsonl`,
  );
  await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  const marker = "SYNTHETIC_FRAGMENT_CREDENTIAL";
  const text = JSON.stringify({
    visible: "PUBLIC_FIRST_FRAGMENT",
    aws_secret_access_key: marker,
  });
  const content =
    variant === "json-string-control"
      ? text
      : [
          { type: "text", text },
          variant === "two-json-text-blocks"
            ? {
                type: "text",
                text: JSON.stringify({
                  visible: "PUBLIC_SECOND_FRAGMENT",
                  clientSecretKey: marker,
                }),
              }
            : {
                type: "tool_use",
                name: "Read",
                input: { path: "public.txt", clientSecretKey: marker },
              },
        ];
  const rows = [
    { role: "user", message: { content: "safe fragment privacy prompt" } },
    { role: "assistant", message: { content } },
    { role: "assistant", message: { content: "finished fragment test" } },
    { type: "turn_ended" },
  ];
  await fs.writeFile(file, rows.map(JSON.stringify).join("\n") + "\n", {
    mode: 0o600,
  });
  const before = readSpans().length;
  for (const [name, data] of [
    ["beforeSubmitPrompt", { prompt: "safe fragment privacy prompt" }],
    ["afterAgentResponse", { text: "finished fragment test" }],
    ["stop", { status: "completed", input_tokens: 100, output_tokens: 10 }],
  ])
    await send(event(conversation, name, { ...data, transcript_path: file }));
  const all = readSpans().slice(before);
  const generations = all.filter((s) => attrs(s)["fi.span.kind"] === "LLM");
  assert.equal(all.length, 3);
  assert.equal(generations.length, 2);
  const first = generations.find((s) =>
    (attrs(s)["output.value"] ?? "").includes("PUBLIC_FIRST_FRAGMENT"),
  );
  const last = generations.find(
    (s) => attrs(s)["output.value"] === "finished fragment test",
  );
  assert.ok(first, "preserve public generation output");
  assert.ok(last, "preserve the following generation");
  for (const text of [
    attrs(first)["output.value"],
    attrs(last)["input.value"],
  ]) {
    assert.match(text, /PUBLIC_FIRST_FRAGMENT/);
    assert.match(text, /\[REDACTED\]/);
    assert.ok(!text.includes(marker));
    if (variant === "two-json-text-blocks")
      assert.match(text, /PUBLIC_SECOND_FRAGMENT/);
    if (variant === "json-with-tool-call") assert.match(text, /tool_calls/);
  }
  assert.ok(!JSON.stringify(all).includes(marker));
  return {
    case: variant,
    spanCount: all.length,
    leakingOutputs: 0,
    leakingFollowingInputs: 0,
  };
}
module.exports = {
  fragmentRedactionCases,
  fragmentRedactionRegression,
  failurePayloads,
  errorContextRegression,
  boundaryCases,
  boundaryRegression,
};
