// Run only in an isolated cloud sandbox after dependency downloads; block non-loopback egress.
const assert = require("node:assert/strict");
const { SemanticConventions: SC } = require("@traceai/fi-semantic-conventions");
const fs = require("node:fs/promises");
const path = require("node:path");
const http = require("node:http");
const { spawn } = require("node:child_process");
const crypto = require("node:crypto");
const cli =
  process.env.AGENTS_TEST_CLI ||
  path.resolve(__dirname, "../../traceai_agents/dist/cli.js");
const out = process.env.EVIDENCE_DIR || "/work/evidence";
const hostPath =
  process.env.COPILOT_TEST_BIN_DIR || "/work/host/node_modules/.bin";
const API = "synthetic-fi-api-acceptance",
  SECRET = "synthetic-fi-secret-acceptance";
const results = [];
let modelCalls = 0;
const transformer = require.resolve("@opentelemetry/otlp-transformer", {
  paths: [path.resolve(__dirname, "../../traceai_agent_core")],
});
const protoRequest = require(
  path.join(path.dirname(transformer), "generated/root.js"),
).opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
const flatten = (body) =>
  (body.resourceSpans || []).flatMap((r) =>
    (r.scopeSpans || []).flatMap((s) => s.spans || []),
  );
const attrs = (s) =>
  Object.fromEntries(
    (s.attributes || []).map((a) => [a.key, Object.values(a.value)[0]]),
  );
const listen = (s) => new Promise((r) => s.listen(0, "127.0.0.1", r));
function invoke(args, env, cwd, timeout = 45000, onSpawn = () => {}) {
  return new Promise((resolve, reject) => {
    const p = spawn(process.execPath, [cli, ...args], { env, cwd });
    onSpawn(p);
    let stdout = "",
      stderr = "";
    p.stdout.on("data", (d) => (stdout += d));
    p.stderr.on("data", (d) => (stderr += d));
    p.stdin.end();
    const timer = setTimeout(() => p.kill("SIGKILL"), timeout);
    p.on("error", reject);
    p.on("close", (code, signal) => {
      clearTimeout(timer);
      resolve({ code, signal, stdout, stderr });
    });
  });
}
async function main() {
  await fs.mkdir(out, { recursive: true });
  for (const scenario of [
    "content",
    "private",
    "protobuf",
    "disabled",
    "rejected",
    "mid-disable",
    "project",
    "provider-file",
    "resume",
    "broken-config",
    "hook",
    "tool-error",
    "tool-error-hidden",
    "unapproved-project-env",
    "hidden-outputs",
    "attached-directory",
    "delayed-collector",
    "unsafe-project",
    "interrupt",
    "interrupt-private",
    "mid-uninstall-credentials",
    "mid-uninstall-project",
    "mid-uninstall-settings",
  ]) {
    const interrupted = scenario.startsWith("interrupt");
    const privateMode =
      scenario === "private" || scenario === "interrupt-private";
    const midUninstall = scenario.startsWith("mid-uninstall");
    const core = require(
      require.resolve("@traceai/agent-core", { paths: [path.dirname(cli)] }),
    );
    let activeLauncher,
      snapshot,
      signalSent = false;
    const home = await fs.mkdtemp("/work/copilot-" + scenario + "-");
    const work = path.join(home, "work");
    await fs.mkdir(work);
    await fs.mkdir(path.join(home, ".copilot"), { mode: 0o700 });
    const captures = [],
      requests = [];
    let probeCalls = 0,
      fenceCount = 0;
    let env;
    const collector = http.createServer((q, r) => {
      const chunks = [];
      q.on("data", (c) => chunks.push(c));
      q.on("end", () => {
        const bytes = Buffer.concat(chunks);
        const proto = q.headers["content-type"] === "application/x-protobuf";
        const body = proto
          ? protoRequest.toObject(protoRequest.decode(bytes), {
              longs: String,
              bytes: String,
            })
          : JSON.parse(bytes);
        if (body?.resourceSpans?.length === 0) probeCalls++;
        else captures.push({ headers: q.headers, body, bytes });
        assert.equal(q.headers["x-api-key"], API);
        assert.equal(q.headers["x-secret-key"], SECRET);
        r.statusCode =
          scenario === "rejected" && body?.resourceSpans?.length !== 0
            ? 401
            : 200;
        r.setHeader(
          "content-type",
          proto ? "application/x-protobuf" : "application/json",
        );
        const acknowledge = () => r.end(proto ? Buffer.alloc(0) : "{}");
        if (scenario === "delayed-collector" && body?.resourceSpans?.length)
          setTimeout(acknowledge, captures.length === 1 ? 3000 : 100);
        else acknowledge();
      });
    });
    await listen(collector);
    const model = http.createServer((q, r) => {
      let b = "";
      q.on("data", (c) => (b += c));
      q.on("end", async () => {
        try {
          const req = JSON.parse(b);
          requests.push({
            path: q.url,
            model: req.model,
            roles: (req.messages || []).map((m) => m.role),
          });
          modelCalls++;
          assert.ok(q.url.endsWith("/chat/completions"));
          assert.equal(req.model, "gpt-4.1");
          const toolDone = req.messages?.some((m) => m.role === "tool");
          let answer;
          if (!toolDone) {
            assert.ok(req.tools.some((t) => t.function?.name === "bash"));
            answer = {
              role: "assistant",
              content: null,
              tool_calls: [
                {
                  index: 0,
                  id: "call_isolation",
                  type: "function",
                  function: {
                    name: "bash",
                    arguments: JSON.stringify({
                      command: `node -e 'const fs=require("fs");const keys=["FI_API_KEY","FI_SECRET_KEY","OTEL_EXPORTER_OTLP_HEADERS","OTEL_EXPORTER_OTLP_TRACES_HEADERS"];fs.writeFileSync("child-isolation.json",JSON.stringify(Object.fromEntries(keys.map(k=>[k,process.env[k]===undefined]))));console.log(Buffer.from("Zml4dHVyZS10b29sLXJlc3VsdA==","base64").toString())'${scenario.startsWith("tool-error") ? "; printf UFJJVkFURV9UT09MX0VSUk9SX0RFVEFJTA== | base64 -d >&2; exit 17" : ""}`,
                      description: "Check synthetic environment isolation",
                    }),
                  },
                },
              ],
            };
          } else {
            if (interrupted) {
              setTimeout(() => {
                signalSent = activeLauncher.kill("SIGINT");
              }, 300);
              return; // Interrupt a genuinely pending native model request.
            }
            if (midUninstall) {
              const malformed =
                scenario === "mid-uninstall-credentials"
                  ? path.join(env.FI_AGENT_CONFIG_DIR, "credentials.json")
                  : scenario === "mid-uninstall-project"
                    ? path.join(work, ".copilot/futureagi.json")
                    : path.join(env.COPILOT_HOME, "futureagi.json");
              await fs.mkdir(path.dirname(malformed), {
                recursive: true,
                mode: 0o700,
              });
              await fs.writeFile(malformed, "{broken json", { mode: 0o600 });
              const removed = await invoke(
                ["uninstall", "copilot", "--json"],
                env,
                work,
              );
              assert.equal(removed.code, 0, removed.stdout + removed.stderr);
              assert.equal(core.collectionEnabled(snapshot), false);
              await assert.rejects(
                fs.stat(
                  path.join(env.FI_AGENT_CONFIG_DIR, "copilot-launcher.json"),
                ),
                { code: "ENOENT" },
              );
              fenceCount = captures.length;
            }
            if (scenario === "delayed-collector")
              await new Promise((resolve) => setTimeout(resolve, 1200));
            if (scenario === "mid-disable") {
              const disabled = await invoke(
                ["disable", "copilot", "--json"],
                env,
                work,
              );
              assert.equal(disabled.code, 0);
              fenceCount = captures.length;
            }
            answer = {
              role: "assistant",
              content:
                "fixture-model-output " + SECRET + " " + "x".repeat(24000),
            };
          }
          r.writeHead(200, { "content-type": "text/event-stream" });
          r.write(
            "data: " +
              JSON.stringify({
                id: "chatcmpl-" + requests.length,
                object: "chat.completion.chunk",
                created: 1,
                model: "gpt-4.1",
                choices: [{ index: 0, delta: answer, finish_reason: null }],
              }) +
              "\n\n",
          );
          r.write(
            "data: " +
              JSON.stringify({
                id: "chatcmpl-" + requests.length,
                object: "chat.completion.chunk",
                choices: [
                  {
                    index: 0,
                    delta: {},
                    finish_reason: answer.tool_calls ? "tool_calls" : "stop",
                  },
                ],
                usage: {
                  prompt_tokens: 30,
                  completion_tokens: 6,
                  total_tokens: 36,
                  prompt_tokens_details: { cached_tokens: 5 },
                  completion_tokens_details: { reasoning_tokens: 2 },
                },
              }) +
              "\n\n",
          );
          r.end("data: [DONE]\n\n");
        } catch (e) {
          r.statusCode = 500;
          r.end("{}");
          console.error(e);
        }
      });
    });
    await listen(model);
    try {
      env = {
        PATH: hostPath + ":" + process.env.PATH,
        HOME: home,
        COPILOT_HOME: path.join(home, ".copilot"),
        FI_AGENT_CONFIG_DIR: path.join(home, ".config/futureagi/agents"),
        XDG_STATE_HOME: path.join(home, ".local/state"),
        FI_API_KEY: API,
        FI_SECRET_KEY: SECRET,
        FI_OTLP_ENDPOINT: `http://127.0.0.1:${collector.address().port}/tracer/v1/traces`,
        COPILOT_PROVIDER_BASE_URL: `http://127.0.0.1:${model.address().port}/v1`,
        COPILOT_MODEL: "gpt-4.1",
        COPILOT_PROVIDER_WIRE_API: "completions",
        OTEL_EXPORTER_OTLP_TRACES_ENDPOINT:
          "https://must-not-contact.invalid/traces",
        OTEL_EXPORTER_OTLP_HEADERS: "X-Secret-Key=synthetic-old",
        OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT: "SPAN_AND_EVENT",
      };
      const setupArgs = [
        "setup",
        "copilot",
        "--project",
        "coding-agents",
        "--json",
      ];
      if (privateMode) setupArgs.push("--no-capture-content");
      const setup = await invoke(setupArgs, env, work);
      assert.equal(setup.code, 0, setup.stdout + setup.stderr);
      assert.equal(JSON.parse(setup.stdout).registered, true);
      assert.ok(!setup.stdout.includes(SECRET));
      // Repeated setup updates the owned registration, not shell/Copilot settings.
      const again = await invoke(setupArgs, env, work);
      assert.equal(again.code, 0, again.stdout + again.stderr);
      assert.ok(probeCalls >= 2);
      const credStat = await fs.stat(
        path.join(env.FI_AGENT_CONFIG_DIR, "credentials.json"),
      );
      assert.equal(credStat.mode & 0o777, 0o600);
      if (scenario === "project") {
        const projectSetup = await invoke(
          [
            "setup",
            "copilot",
            "--scope",
            "project",
            "--project",
            "project-only",
            "--json",
          ],
          env,
          work,
        );
        assert.equal(
          projectSetup.code,
          0,
          projectSetup.stdout + projectSetup.stderr,
        );
        const savedOnly = { ...env };
        for (const key of ["FI_API_KEY", "FI_SECRET_KEY", "FI_OTLP_ENDPOINT"])
          delete savedOnly[key];
        const repeated = await invoke(
          ["setup", "copilot", "--scope", "project", "--json"],
          savedOnly,
          work,
        );
        assert.equal(repeated.code, 0, repeated.stdout + repeated.stderr);
        const scopedStatus = await invoke(
          ["status", "copilot", "--json"],
          savedOnly,
          work,
        );
        assert.equal(
          JSON.parse(scopedStatus.stdout).projectName,
          "project-only",
        );
        assert.equal(
          JSON.parse(scopedStatus.stdout).registration,
          "REGISTERED",
        );
      }
      if (scenario === "disabled") {
        const d = await invoke(["disable", "copilot", "--json"], env, work);
        assert.equal(d.code, 0);
      }
      if (scenario === "tool-error-hidden") env.FI_HIDE_ERROR_DETAILS = "true";
      if (scenario === "hidden-outputs") env.FI_HIDE_OUTPUTS = "true";
      let runWork = work;
      const runOptions = [];
      if (scenario === "attached-directory") {
        runWork = path.join(work, "disabled-child");
        await fs.mkdir(path.join(runWork, ".copilot"), { recursive: true });
        await fs.writeFile(
          path.join(runWork, ".copilot/futureagi.json"),
          JSON.stringify({
            schemaVersion: 1,
            enabled: false,
            captureContent: false,
          }),
          { mode: 0o600 },
        );
        const blocked = await invoke(
          [
            "run",
            "copilot",
            "--",
            "-C" + runWork,
            "--disable-builtin-mcps",
            "--no-custom-instructions",
            "-p",
            "must not launch",
            "--allow-all-tools",
            "--no-color",
          ],
          env,
          work,
        );
        assert.equal(blocked.code, 2);
        assert.match(blocked.stderr, /--project-dir/);
        assert.equal(requests.length, 0);
        assert.equal(captures.length, 0);
        runOptions.push("--project-dir", runWork);
      }
      if (scenario === "unsafe-project") {
        await fs.mkdir(path.join(work, ".copilot"));
        await fs.writeFile(
          path.join(work, ".copilot/futureagi.json"),
          JSON.stringify({
            schemaVersion: 1,
            transport: { endpoint: env.FI_OTLP_ENDPOINT + "/unapproved" },
          }),
          { mode: 0o600 },
        );
        await fs.chmod(work, 0o775);
        // Exercise the protected saved profile, not an explicit route override.
        delete env.FI_OTLP_ENDPOINT;
        delete env.FI_API_KEY;
        delete env.FI_SECRET_KEY;
      }
      if (scenario === "unapproved-project-env") {
        await fs.mkdir(path.join(work, ".copilot"));
        await fs.writeFile(
          path.join(work, ".copilot/futureagi.json"),
          JSON.stringify({
            schemaVersion: 1,
            enabled: true,
            transport: { endpoint: env.FI_OTLP_ENDPOINT + "/unapproved" },
          }),
          { mode: 0o600 },
        );
        delete env.FI_OTLP_ENDPOINT;
      }
      if (scenario === "protobuf")
        env.OTEL_EXPORTER_OTLP_PROTOCOL = "http/protobuf";
      if (scenario === "broken-config")
        await fs.writeFile(
          path.join(env.FI_AGENT_CONFIG_DIR, "credentials.json"),
          "{broken json",
        );
      if (scenario === "provider-file") {
        await fs.writeFile(
          path.join(env.COPILOT_HOME, "providers.json"),
          JSON.stringify({
            providers: [
              {
                name: "local",
                type: "openai",
                baseUrl: env.COPILOT_PROVIDER_BASE_URL,
                wireApi: "completions",
              },
            ],
            models: [{ id: "gpt-4.1", provider: "local" }],
          }),
          { mode: 0o600 },
        );
        delete env.COPILOT_PROVIDER_BASE_URL;
        delete env.COPILOT_PROVIDER_WIRE_API;
        env.COPILOT_MODEL = "local/gpt-4.1";
      }
      if (scenario === "hook") {
        await fs.mkdir(path.join(env.COPILOT_HOME, "hooks"));
        await fs.writeFile(
          path.join(env.COPILOT_HOME, "hooks/acceptance.json"),
          JSON.stringify({
            version: 1,
            hooks: {
              preToolUse: [
                {
                  type: "command",
                  bash: "printf hook-fired > " + path.join(work, "hook-fired"),
                  timeoutSec: 5,
                },
              ],
            },
          }),
          { mode: 0o600 },
        );
      }
      const expectedSpans = scenario === "resume" ? 6 : 4;
      const started = Date.now();
      const session = crypto.randomUUID();
      if (midUninstall) {
        snapshot = await core.resolveConfig({
          agent: "copilot",
          home,
          env,
          projectDir: work,
        });
        assert.equal(core.collectionEnabled(snapshot), true);
      }
      const run = await invoke(
        [
          "run",
          "copilot",
          ...runOptions,
          "--",
          "--session-id",
          session,
          "--disable-builtin-mcps",
          "--no-custom-instructions",
          "-p",
          "fixture-user-prompt " + API,
          "--allow-all-tools",
          "--no-color",
        ],
        env,
        work,
        45000,
        (child) => {
          activeLauncher = child;
        },
      );
      assert.equal(run.code, interrupted ? 130 : 0, run.stderr);
      assert.equal(requests.length, 2, JSON.stringify(requests));
      if (!interrupted) assert.match(run.stdout, /fixture-model-output/);
      else assert.equal(signalSent, true);
      assert.ok(
        Object.values(
          JSON.parse(
            await fs.readFile(
              path.join(runWork, "child-isolation.json"),
              "utf8",
            ),
          ),
        ).every((v) => v === true),
      );
      if (scenario === "resume") {
        const again = await invoke(
          [
            "run",
            "copilot",
            "--",
            "--disable-builtin-mcps",
            "--no-custom-instructions",
            "--session-id",
            session,
            "-p",
            "fixture-second-prompt",
            "--allow-all-tools",
            "--no-color",
          ],
          env,
          work,
        );
        assert.equal(again.code, 0, again.stderr);
        assert.equal(requests.length, 3);
      }
      const statusRun = await invoke(
        ["status", "copilot", ...runOptions, "--json"],
        env,
        work,
      );
      const status = JSON.parse(statusRun.stdout);
      assert.ok(
        !statusRun.stdout.includes(API) && !statusRun.stdout.includes(SECRET),
      );
      let spanNames = [],
        spanCount = 0;
      if (
        scenario === "disabled" ||
        midUninstall ||
        scenario === "mid-disable" ||
        scenario === "broken-config" ||
        scenario === "unapproved-project-env" ||
        scenario === "attached-directory"
      ) {
        assert.equal(
          captures.length,
          scenario === "mid-disable" || midUninstall ? fenceCount : 0,
        );
      } else if (scenario === "rejected") {
        // A 401 fences future batches. Depending on native completion timing,
        // the first rejected batch may contain only a tool or the full turn.
        // The host must still finish both requests without retrying export.
        assert.equal(captures.length, 1, "do not retry rejected credentials");
        const spans = flatten(captures[0].body);
        spanCount = spans.length;
        spanNames = spans.map(s => s.name);
        assert.ok(spanCount > 0 && spanCount <= expectedSpans);
        assert.equal(status.delivery.exported, 0);
        assert.equal(status.delivery.lastError, "AUTH_REJECTED");
        assert.ok(!JSON.stringify(captures[0].body).includes(API));
        assert.ok(!JSON.stringify(captures[0].body).includes(SECRET));
      } else if (scenario === "protobuf") {
        assert.ok(captures.length > 0);
        assert.ok(
          captures.every(
            (c) => c.headers["content-type"] === "application/x-protobuf",
          ),
        );
        for (const c of captures) {
          assert.ok(!c.bytes.includes(Buffer.from(API)));
          assert.ok(!c.bytes.includes(Buffer.from(SECRET)));
        }
        // Batching is timing-dependent; a request may flush before the root ends.
        const bytes = Buffer.concat(captures.map((c) => c.bytes));
        assert.ok(bytes.includes(Buffer.from("invoke_agent")));
        assert.ok(bytes.includes(Buffer.from("execute_tool")));
        assert.ok(bytes.includes(Buffer.from("chat gpt-4.1")));
        const spans = captures.flatMap((c) => flatten(c.body));
        spanCount = spans.length;
        spanNames = spans.map((s) => s.name);
        assert.equal(spanCount, 4);
        assert.equal(status.delivery.exported, 4);
        assert.equal(new Set(spans.map((s) => s.traceId)).size, 1);
        assert.equal(new Set(spans.map((s) => attrs(s)["session.id"])).size, 1);
      } else {
        const spans = captures.flatMap((c) => flatten(c.body));
        spanCount = spans.length;
        spanNames = spans.map((s) => s.name);
        assert.equal(spans.length, expectedSpans, JSON.stringify(spanNames));
        assert.equal(
          new Set(spans.map((s) => s.traceId)).size,
          scenario === "resume" ? 2 : 1,
        );
        const roots = spans.filter((s) => !s.parentSpanId);
        assert.equal(roots.length, scenario === "resume" ? 2 : 1);
        assert.ok(
          roots.every(
            (s) => attrs(s)["gen_ai.operation.name"] === "invoke_agent",
          ),
        );
        for (const root of roots)
          assert.equal(attrs(root)[SC.FI_AGENT_OUTCOME], interrupted ? "interrupted" : "success");
        for (const s of spans.filter((s) => s.parentSpanId))
          assert.ok(
            spans.some(
              (parent) =>
                parent.spanId === s.parentSpanId &&
                parent.traceId === s.traceId,
            ),
          );
        assert.equal(new Set(spans.map((s) => attrs(s)["session.id"])).size, 1);
        assert.ok(spans.every((s) => attrs(s)["user.id"]));
        const raw = JSON.stringify(captures.map((c) => c.body));
        assert.ok(!raw.includes(API));
        assert.ok(!raw.includes(SECRET));
        if (interrupted) {
          const aborted = spans.filter(
            (s) => attrs(s)[SC.FI_AGENT_OUTCOME] === "interrupted",
          );
          assert.equal(
            aborted.length,
            2,
            "only the affected generation and its turn are interrupted",
          );
          const request = aborted.find(
            (s) => attrs(s)["gen_ai.operation.name"] === "chat",
          );
          const turn = aborted.find(
            (s) => attrs(s)["gen_ai.operation.name"] === "invoke_agent",
          );
          assert.ok(request && turn);
          assert.equal(request.parentSpanId, turn.spanId);
          assert.equal(request.traceId, turn.traceId);
          assert.equal(request.status.code ?? 0, 0);
          assert.equal(turn.status.code ?? 0, 0);
          const abort = request.events.find(
            (e) => e.name === "github.copilot.session.abort",
          );
          assert.ok(abort);
          assert.deepEqual(attrs(abort), {
            "github.copilot.abort_reason": "user_initiated",
          });
        }
        if (privateMode) {
          assert.ok(!raw.includes("fixture-user-prompt"));
          assert.ok(!raw.includes("fixture-model-output"));
          assert.ok(!raw.includes("fixture-tool-result"));
          assert.ok(!raw.includes("gen_ai.system_instructions"));
        } else if (scenario === "hidden-outputs") {
          assert.ok(raw.includes("fixture-user-prompt"));
          assert.ok(!raw.includes("fixture-model-output"));
          assert.ok(!raw.includes("fixture-tool-result"));
        } else {
          assert.ok(raw.includes("fixture-user-prompt"));
          assert.equal(raw.includes("fixture-model-output"), !interrupted);
          assert.equal(
            raw.includes("fixture-tool-result"),
            scenario !== "tool-error-hidden",
          );
        }
        for (const s of spans)
          for (const a of s.attributes || [])
            if (a.value.stringValue)
              assert.ok(a.value.stringValue.length <= 20000);
        for (const c of captures)
          assert.equal(
            c.headers["x-project-name"],
            scenario === "project" ? "project-only" : "coding-agents",
          );
        if (scenario === "hook") {
          assert.equal(
            await fs.readFile(path.join(work, "hook-fired"), "utf8"),
            "hook-fired",
          );
          assert.equal(
            spans.filter(
              (s) => attrs(s)["gen_ai.operation.name"] === "execute_hook",
            ).length,
            0,
            "Pinned host emits no hook span; do not invent one",
          );
        }
        if (scenario.startsWith("tool-error")) {
          const tool = spans.find(
            (s) => attrs(s)["gen_ai.operation.name"] === "execute_tool",
          );
          assert.equal(attrs(tool)["process.exit.code"], 17);
          assert.equal(tool.status.code, 2);
          if (scenario === "tool-error-hidden") {
            assert.equal(attrs(tool)["gen_ai.tool.call.result"], undefined);
            assert.equal(attrs(tool)[SC.OUTPUT_VALUE], undefined);
            assert.ok(!raw.includes("PRIVATE_TOOL_ERROR_DETAIL"));
            assert.equal(tool.status.message, undefined);
          } else
            assert.match(
              attrs(tool)[SC.OUTPUT_VALUE],
              /PRIVATE_TOOL_ERROR_DETAIL/,
            );
        }
        assert.equal(status.delivery.exported, expectedSpans);
        assert.ok(status.delivery.lastAcceptedAt);
        assert.equal(status.activation, "OBSERVED");
      }
      if (scenario === "delayed-collector") {
        assert.ok(
          captures.length >= 2,
          "regression requires an export in flight before the final batch",
        );
        assert.equal(status.delivery.dropped, 0);
      }
      if (scenario === "unsafe-project")
        assert.ok(status.issues.includes("PROJECT_CONFIG_IGNORED"));
      if (scenario === "unapproved-project-env") {
        assert.ok(status.issues.includes("DESTINATION_NOT_APPROVED"));
        assert.equal(
          probeCalls,
          2,
          "untrusted destination must never receive even a credential probe",
        );
      }
      if (captures.length && scenario !== "rejected" && scenario !== "mid-disable" && !midUninstall) {
        const spans = captures.flatMap((c) => flatten(c.body));
        let measured = 0;
        for (const span of spans) {
          const a = attrs(span);
          const seconds = a["gen_ai.response.time_to_first_chunk"];
          if (
            a["gen_ai.operation.name"] === "chat" &&
            typeof seconds === "number" &&
            Number.isFinite(seconds)
          ) {
            assert.equal(
              a[SC.GEN_AI_SERVER_TIME_TO_FIRST_TOKEN],
              seconds * 1000,
              "native seconds must map to canonical milliseconds",
            );
            measured++;
          } else
            assert.equal(a[SC.GEN_AI_SERVER_TIME_TO_FIRST_TOKEN], undefined);
        }
        assert.ok(
          measured > 0,
          "real Copilot must provide measured request TTFT",
        );
        for (const [key, perRequest] of [
          [SC.LLM_TOKEN_COUNT_PROMPT, 30],
          [SC.LLM_TOKEN_COUNT_COMPLETION, 6],
          [SC.LLM_TOKEN_COUNT_TOTAL, 36],
        ]) {
          const total = spans.reduce(
            (n, s) => n + Number(attrs(s)[key] ?? 0),
            0,
          );
          assert.equal(
            total,
            (requests.length - (interrupted ? 1 : 0)) * perRequest,
            `${scenario}: additive ${key}`,
          );
          for (const s of spans.filter(
            (s) => attrs(s)["gen_ai.operation.name"] !== "chat",
          ))
            assert.equal(attrs(s)[key], undefined);
        }
      }
      const stateFiles = await fs
        .readdir(path.join(home, ".local/state/futureagi/agents"))
        .catch(() => []);
      assert.ok(!stateFiles.some((n) => n.startsWith("copilot-run-")));
      const uninstalled = await invoke(
        ["uninstall", "copilot", "--json"],
        env,
        work,
      );
      assert.equal(
        uninstalled.code,
        0,
        uninstalled.stdout + uninstalled.stderr,
      );
      results.push({
        scenario,
        hostVersion: "1.0.106",
        exitCode: run.code,
        durationMs: Date.now() - started,
        modelRequests: requests.length,
        spans: spanCount,
        spanNames,
        ...(scenario === "hook"
          ? { hookExecuted: true, nativeHookSpans: 0 }
          : {}),
        content: !privateMode,
        ...(interrupted ? { signalSent, cancellationSemantics: true } : {}),
        ...(midUninstall
          ? { activeSessionFenced: true, registrationRemoved: true }
          : {}),
        protocol: scenario === "protobuf" ? "http/protobuf" : "http/json",
        credentialsExcludedFromTools: true,
        delivery: status.delivery,
      });
      await fs.writeFile(
        path.join(out, scenario + "-capture.json"),
        JSON.stringify(
          captures.map((c) => ({
            contentType: c.headers["content-type"],
            project: c.headers["x-project-name"],
            body: c.body,
            bytes: c.bytes.length,
          })),
          null,
          2,
        ),
      );
    } finally {
      model.closeAllConnections();
      await new Promise((r) => model.close(r));
      await new Promise((r) => collector.close(r));
      await fs.writeFile(
        path.join(out, "result.json"),
        JSON.stringify(
          { hostVersion: "1.0.106", modelCalls, results },
          null,
          2,
        ),
      );
    }
  }
  console.log(
    JSON.stringify(
      { passed: true, hostVersion: "1.0.106", modelCalls, results },
      null,
      2,
    ),
  );
}
main().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
