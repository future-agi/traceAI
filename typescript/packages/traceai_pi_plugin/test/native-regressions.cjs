const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { fork } = require("node:child_process");
const { once } = require("node:events");
const attrs = (s) =>
  Object.fromEntries(
    s.attributes.map((a) => [a.key, Object.values(a.value)[0]]),
  );

exports.routingAndCredentials = async ({
  base,
  cwd,
  env,
  run,
  spans,
  requests,
  regressions,
}) => {
  const router = path.join(base, "failing-route.mjs");
  await fs.writeFile(
    router,
    `export default function(pi){pi.registerVirtualModel({provider:'failure-router',id:'auto',name:'Failure Router',contextWindow:100000,maxTokens:1024,route(){throw new Error('ROUTE_FAILURE_CANARY')}});}`,
  );
  const knownSession = attrs(spans().find((s) => !s.parentSpanId))[
    "session.id"
  ];
  for (const flag of [
    undefined,
    "FI_HIDE_ERROR_DETAILS",
    "FI_HIDE_OUTPUTS",
    "FI_AGENT_CAPTURE_CONTENT",
  ]) {
    const offset = spans().length;
    const beforeRequests = requests.length;
    if (flag)
      env[flag] = flag === "FI_AGENT_CAPTURE_CONTENT" ? "false" : "true";
    try {
      const events = await run("ROUTING_FAILURE_PROMPT", [
        "-e",
        router,
        "--provider",
        "failure-router",
        "--model",
        "auto",
      ]);
      assert.equal(
        events.find(
          (e) => e.type === "message_end" && e.message?.role === "assistant",
        ).message.stopReason,
        "error",
      );
      assert.equal(
        events.find((e) => e.type === "agent_settled").aborted,
        false,
      );
    } finally {
      if (flag) delete env[flag];
    }
    const observed = spans().slice(offset);
    assert.equal(
      requests.length,
      beforeRequests,
      "routing failed before provider request",
    );
    assert.equal(
      observed.length,
      1,
      "exactly one root, zero generations or other fabricated work",
    );
    const root = observed[0];
    assert.equal(root.parentSpanId, undefined);
    assert.equal(root.status.code, 2);
    assert.equal(attrs(root)["fi.agent.outcome"], "error");
    assert.equal(attrs(root)["session.id"], knownSession);
    assert.equal(
      attrs(root)["input.value"],
      flag === "FI_AGENT_CAPTURE_CONTENT"
        ? undefined
        : "ROUTING_FAILURE_PROMPT",
    );
    assert.equal(attrs(root)["gen_ai.usage.input_tokens"], undefined);
    assert.equal(attrs(root)["gen_ai.usage.output_tokens"], undefined);
    assert.equal(attrs(root)["gen_ai.cost.total"], undefined);
    if (flag)
      assert(!JSON.stringify(observed).includes("ROUTE_FAILURE_CANARY"));
    else assert.equal(attrs(root)["output.value"], "ROUTE_FAILURE_CANARY");
  }
  regressions.preProviderRoutingFailure = 4;
  const sample = path.join(cwd, "sample.txt");
  const original = await fs.readFile(sample, "utf8");
  const credentials =
    'AWS_ACCESS_KEY_ID=ACCESS_ALIAS_CANARY\nAWS_SECRET_ACCESS_KEY: SECRET_ALIAS_CANARY\nAuthorization: Basic QkFTSUNfQ0FOQVJZ\nclientSecret="QUOTED_CANARY"\naccess_token: |\n  MULTILINE_CANARY';
  const canaries = [
    "ACCESS_ALIAS_CANARY",
    "SECRET_ALIAS_CANARY",
    "QkFTSUNfQ0FOQVJZ",
    "QUOTED_CANARY",
    "MULTILINE_CANARY",
  ];
  const offset = spans().length;
  const beforeRequests = requests.length;
  try {
    await fs.writeFile(sample, credentials);
    await run(
      `TOOL_LOOP: credential privacy prompt\n${credentials}`,
      [],
      "credential-alias-session",
    );
    await run(
      "Follow up on the previous tool output.",
      [],
      "credential-alias-session",
    );
  } finally {
    await fs.writeFile(sample, original);
  }
  const observed = spans().slice(offset);
  assert.equal(observed.filter((s) => !s.parentSpanId).length, 2);
  assert.equal(observed.filter((s) => s.name === "pi.generation").length, 3);
  assert.equal(observed.filter((s) => s.name === "read").length, 1);
  const replay = JSON.stringify(requests.slice(beforeRequests).at(-1).messages);
  for (const canary of canaries) {
    assert(
      replay.includes(canary),
      "real provider replay contains synthetic canary",
    );
    assert(
      !JSON.stringify(observed).includes(canary),
      `OTLP must not contain synthetic canary ${canary}`,
    );
  }
  assert(
    attrs(observed.find((s) => s.name === "read"))["output.value"].includes(
      "REDACTED",
    ),
  );
  assert(
    observed
      .filter((s) => s.name === "pi.generation")
      .every((s) => attrs(s)["input.value"]),
  );
  regressions.credentialAliasOtlp = true;
};
exports.killWriter = async (base, env) => {
  const manifest = path.join(env.FI_AGENT_CONFIG_DIR, "installation.json");
  const before = await fs.readFile(manifest, "utf8");
  const child = fork(
    path.resolve(
      __dirname,
      "../../traceai_agent_core/test/forks/pi/fixtures/hold-write.cjs",
    ),
    [
      manifest,
      path.join(base, "cli/node_modules/@traceai/agent-core/dist/src/index.js"),
    ],
    { env, stdio: ["ignore", "ignore", "inherit", "ipc"] },
  );
  try {
    const [message] = await once(child, "message", {
      signal: AbortSignal.timeout(10000),
    });
    assert.equal(message.type, "held");
    const exit = once(child, "exit");
    child.kill("SIGKILL");
    await exit;
    assert.equal(await fs.readFile(manifest, "utf8"), before);
    assert((await fs.lstat(`${manifest}.write-lock`)).isDirectory());
  } finally {
    if (child.exitCode === null) child.kill("SIGKILL");
  }
};
