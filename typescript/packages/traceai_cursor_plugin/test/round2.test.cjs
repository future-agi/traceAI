const { test } = require("node:test");
const assert = require("node:assert/strict");
const { fixture, fs, path, child, core } = require("./helpers.cjs");
const {
  load,
  attrs,
  spans,
  prepare,
  checkGolden,
} = require("./round2-golden.cjs");
const { normalizeEvent } = require("../dist/events.js");
const { CursorObserver } = require("../dist/observer.js");
const { TranscriptReader, correlate } = require("../dist/transcript.js");
const {
  installRuntime,
  register,
  commands,
  pinnedInterpreter,
  inspect,
} = require("../dist/registration.js");

for (const label of ["run4", "cli", "cli2", "cli-print"])
  test(`golden ${label}: real sanitized hooks, fixture transcript, duplicate terminals`, async (t) => {
    const f = await fixture(t),
      late = label === "cli2",
      transcript = await prepare(f.home, label, late);
    const runtime = await installRuntime(f.context);
    await register(f.context, runtime);
    const hooks = commands(runtime, f.config.paths.configDir);
    let write;
    t.after(async () => {
      if (write) await write;
    });
    for (const row of load(label)) {
      if (late && row.hook_event_name === "stop") {
        // The snapshot is claimed before the transcript has any assistant row: reproduce the Mac race.
        write = new Promise((r) => setTimeout(r, 6000)).then(() =>
          fs.appendFile(transcript.file, transcript.tail),
        );
      }
      const result = await child(
        hooks[row.hook_event_name].command,
        {
          ...row,
          workspace_roots: [f.home],
          transcript_path: row.transcript_path ? transcript.file : null,
        },
        { HOME: f.home, PATH: "/missing", NODE_OPTIONS: "" },
      );
      assert.equal(result.code, 0);
      assert.equal(result.stdout, "");
      assert.equal(result.stderr, "");
      assert.ok(result.ms < 8000);
      if (late && row.hook_event_name === "stop") {
        assert.ok(result.ms < 3000, `late stop blocked for ${result.ms} ms`);
        assert.equal(spans(f.requests).length, 0);
      }
    }
    if (write) {
      await write;
      const deadline = Date.now() + 5000;
      while (spans(f.requests).length < 11 && Date.now() < deadline)
        await new Promise((r) => setTimeout(r, 50));
    }
    const all = spans(f.requests);
    checkGolden(all, label);
    const terminal =
      load(label).find((r) => r.hook_event_name === "stop") ??
      load(label).at(-1);
    await child(
      hooks[terminal.hook_event_name].command,
      {
        ...terminal,
        workspace_roots: [f.home],
        transcript_path: transcript.file,
      },
      { HOME: f.home, PATH: "/missing" },
    );
    assert.equal(spans(f.requests).length, all.length);
  });

test("no generation: real cli2 usage stays additive on root exactly once, including content opt-out", async (t) => {
  for (const capture of [true, false]) {
    const f = await fixture(t, capture),
      o = new CursorObserver(f.config);
    const rows = load("cli2");
    for (const row of rows) {
      const e = normalizeEvent(
        { ...row, transcript_path: null },
        row.hook_event_name,
        f.config,
      );
      await o.accept(e);
      if (["stop", "sessionEnd"].includes(e.event)) await o.accept(e);
    }
    await o.close();
    const all = spans(f.requests),
      root = all.find((s) => !s.parentSpanId),
      a = attrs(root);
    assert.equal(all.length, 6);
    assert.equal(a["fi.agent.gap"], "TRANSCRIPT_UNAVAILABLE");
    assert.equal(a["fi.usage.contribution"], "additive");
    assert.equal(a["fi.agent.usage.additive"], true);
    for (const [key, expected] of [
      ["input_tokens", 78759],
      ["output_tokens", 329],
      ["cache_read_tokens", 67584],
      ["total_tokens", 79088],
    ])
      assert.equal(
        all.reduce(
          (n, s) => n + Number(attrs(s)[`gen_ai.usage.${key}`] ?? 0),
          0,
        ),
        expected,
      );
    if (!capture)
      assert.ok(
        all.every(
          (s) =>
            attrs(s)["input.value"] === undefined &&
            attrs(s)["output.value"] === undefined,
        ),
      );
  }
});

test("settling waits for the matching turn, not an earlier terminal; final response can prove completion", async (t) => {
  const f = await fixture(t),
    p = await prepare(f.home, "cli2", true),
    rows = load("cli2");
  const reader = new TranscriptReader(f.config),
    prompt = rows[0].prompt,
    output = rows.find((r) => r.text).text;
  const old =
    '{"role":"user","message":{"content":"old prompt"}}\n{"type":"turn_ended","status":"success"}\n';
  const first = await fs.readFile(p.file, "utf8");
  await fs.writeFile(p.file, old + first);
  const pending = reader.readSettled(p.file, prompt, output);
  await new Promise((r) => setTimeout(r, 350));
  // No terminal line: afterAgentResponse matches the final text-only assistant row.
  await fs.appendFile(
    p.file,
    p.tail.split("\n").slice(0, -2).join("\n") + "\n",
  );
  const result = await pending;
  assert.equal(correlate(result, prompt, output).messages.length, 5);
  assert.equal(correlate(result, prompt), undefined);
});

test("settling is bounded at 5 seconds when Cursor never finishes writing", async (t) => {
  const f = await fixture(t),
    p = await prepare(f.home, "cli2", true);
  const begin = Date.now();
  const result = await new TranscriptReader(f.config).readSettled(
    p.file,
    load("cli2")[0].prompt,
    undefined,
    { budgetMs: 9000 },
  );
  assert.equal(correlate(result, load("cli2")[0].prompt), undefined);
  assert.ok(Date.now() - begin >= 4900);
  assert.ok(Date.now() - begin < 5800);
});

test("generation context and tool-only output are bounded, credential-redacted, and obey privacy flags", async (t) => {
  const f = await fixture(t),
    p = await prepare(f.home, "cli2");
  const rows = [
    { role: "user", message: { content: "prompt synthetic-cursor-secret" } },
    {
      role: "assistant",
      message: {
        content: [
          {
            type: "tool_use",
            name: "Read",
            input: { key: "synthetic-cursor-api", path: "public.py" },
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
            content: "tool-context synthetic-cursor-secret",
          },
        ],
      },
    },
    {
      role: "assistant",
      message: { content: [{ type: "text", text: "answer" }] },
    },
    { type: "turn_ended", status: "success" },
  ];
  await fs.writeFile(p.file, rows.map(JSON.stringify).join("\n") + "\n");
  const result = await new TranscriptReader(f.config).read(p.file);
  assert.equal(result.turns.length, 1);
  const [first, last] = result.turns[0].messages;
  assert.match(first.output, /tool_calls.*Read.*\[REDACTED\]/);
  assert.match(last.input, /tool-context \[REDACTED\]/);
  assert.ok(!JSON.stringify(result).includes("synthetic-cursor-secret"));
  assert.ok(!JSON.stringify(result).includes("synthetic-cursor-api"));
  const hidden = await new TranscriptReader({
    ...f.config,
    hideOutputs: true,
  }).read(p.file);
  assert.equal(hidden.turns[0].messages[0].output, undefined);
  assert.ok(!hidden.turns[0].messages[1].input.includes("tool-context"));
  const noInput = await new TranscriptReader({
    ...f.config,
    hideInputs: true,
  }).read(p.file);
  assert.ok(noInput.turns[0].messages.every((m) => m.input === undefined));
  rows[1].message.content[0].input.large = "x".repeat(30000);
  await fs.writeFile(p.file, rows.map(JSON.stringify).join("\n") + "\n");
  const bounded = await new TranscriptReader(f.config).read(p.file);
  for (const m of bounded.turns[0].messages) {
    assert.ok(m.input.length <= 20000);
    assert.ok(m.output.length <= 20000);
  }
});

test("pin the invoked PATH symlink, survive target upgrade, and diagnose its removal", async (t) => {
  const f = await fixture(t),
    bin = path.join(f.home, ".local/bin"),
    linked = path.join(bin, "node");
  await fs.mkdir(bin, { recursive: true });
  await fs.symlink(process.execPath, linked);
  assert.equal(
    await pinnedInterpreter({
      argv0: "node",
      execPath: process.execPath,
      path: bin,
    }),
    linked,
  );
  assert.equal(
    await pinnedInterpreter({ argv0: linked, execPath: process.execPath }),
    linked,
  );
  assert.equal(
    await pinnedInterpreter({
      argv0: "node",
      execPath: process.execPath,
      path: "/missing",
    }),
    await fs.realpath(process.execPath),
  );
  const wrong = path.join(f.home, "unrelated");
  await fs.writeFile(wrong, "not the running Node", { mode: 0o700 });
  assert.equal(
    await pinnedInterpreter({ argv0: wrong, execPath: process.execPath }),
    await fs.realpath(process.execPath),
  );
  const runtime = await installRuntime(f.context);
  const script = `const core=require('@traceai/agent-core');const reg=require(${JSON.stringify(path.resolve(__dirname, "../dist/registration.js"))});(async()=>{const config=await core.resolveConfig({agent:'cursor',home:process.env.HOME,env:{}});await reg.register({config,scope:'user'},${JSON.stringify(runtime)});})().catch(()=>process.exitCode=1);`;
  // Actual setup runs through the stable symlink, not just a mocked resolver.
  const run = await child(
    `'${linked}' -e '${script.replace(/'/g, `'\\''`)}'`,
    "",
    {
      HOME: f.home,
      PATH: bin,
      NODE_PATH: path.resolve(__dirname, "../node_modules"),
    },
  );
  assert.equal(run.code, 0, run.stderr);
  const receipt = JSON.parse(
    await fs.readFile(
      path.join(f.config.paths.configDir, "cursor-registration.json"),
    ),
  );
  assert.equal(receipt.interpreter, linked);
  assert.ok(receipt.owned[0].stop.command.startsWith(`'${linked}'`));
  assert.equal((await inspect(f.context)).registered, true);
  await fs.unlink(linked);
  // A stable path remains valid after a symlink target upgrade (same available runtime here).
  await fs.symlink(await fs.realpath(process.execPath), linked);
  assert.equal((await inspect(f.context)).registered, true);
  await fs.unlink(linked);
  assert.equal((await inspect(f.context)).detail, "CURSOR_INTERPRETER_MISSING");
  const { cursorAdapter } = require("../dist/index.js");
  assert.ok(
    (
      await core.inspectStatus({ ...f.options, adapter: cursorAdapter })
    ).issues.includes("CURSOR_INTERPRETER_MISSING"),
  );
});

test("real aggregate tokens survive a missing prompt hook or a transcript that never settles", async (t) => {
  for (const missingPrompt of [true, false]) {
    const f = await fixture(t),
      p = await prepare(f.home, "cli2", true),
      o = new CursorObserver(f.config);
    for (const row of load("cli2")) {
      if (missingPrompt && row.hook_event_name === "beforeSubmitPrompt")
        continue;
      await o.accept(
        normalizeEvent(
          { ...row, transcript_path: p.file },
          row.hook_event_name,
          f.config,
        ),
      );
    }
    await o.close();
    const all = spans(f.requests),
      root = all.find((s) => !s.parentSpanId);
    assert.equal(all.length, 6);
    assert.equal(Number(attrs(root)["gen_ai.usage.total_tokens"]), 79088);
    assert.equal(attrs(root)["fi.usage.contribution"], "additive");
    assert.equal(
      attrs(root)["fi.agent.gap"],
      missingPrompt ? "MISSING_PROMPT" : "TRANSCRIPT_CORRELATION_REQUIRED",
    );
  }
});
