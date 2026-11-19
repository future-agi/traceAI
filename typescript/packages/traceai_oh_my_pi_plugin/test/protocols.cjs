const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { spawn } = require("node:child_process");
const delay = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitFor(fn, description) {
  const end = Date.now() + 45000;
  while (Date.now() < end) {
    const v = fn();
    if (v) return v;
    await delay(30);
  }
  throw Error("TIMEOUT " + description);
}
function processFor(mode, env, cwd) {
  const child = spawn(
    "omp",
    ["--mode", mode, "--no-title", "--model", "mock/test", "--tools", "read"],
    { cwd, env, stdio: ["pipe", "pipe", "pipe"] },
  );
  const lines = [];
  let buffer = "",
    stderr = "";
  child.stdout.on("data", (b) => {
    buffer += b;
    let i;
    while ((i = buffer.indexOf("\n")) >= 0) {
      const s = buffer.slice(0, i);
      buffer = buffer.slice(i + 1);
      try {
        lines.push(JSON.parse(s));
      } catch {}
    }
  });
  child.stderr.on("data", (b) => (stderr += b));
  return {
    child,
    lines,
    send: (v) => child.stdin.write(JSON.stringify(v) + "\n"),
    stderr: () => stderr,
    async close() {
      if (child.exitCode !== null) return;
      child.kill("SIGTERM");
      await Promise.race([
        new Promise((r) => child.once("exit", r)),
        delay(3000),
      ]);
      if (child.exitCode === null) child.kill("SIGKILL");
    },
  };
}
exports.runProtocols = async ({ env, cwd, base, spans }) => {
  const results = [];
  for (const mode of ["rpc", "acp"]) {
    const start = spans().length,
      p = processFor(mode, env, cwd);
    try {
      if (mode === "rpc") {
        p.send({ id: "start", type: "prompt", message: "QUEUE_START" });
        await delay(450);
        p.send({ id: "follow", type: "follow_up", message: "QUEUE_FOLLOW" });
        await waitFor(
          () =>
            spans()
              .slice(start)
              .filter((s) => !s.parentSpanId).length >= 2,
          "two queued prompt roots",
        );
        const roots = spans()
          .slice(start)
          .filter((s) => !s.parentSpanId);
        assert.equal(roots.length, 2);
        assert.notEqual(roots[0].traceId, roots[1].traceId);
      } else {
        p.send({
          jsonrpc: "2.0",
          id: 1,
          method: "initialize",
          params: { protocolVersion: 1, clientCapabilities: {} },
        });
        const init = await waitFor(
          () => p.lines.find((x) => x.id === 1),
          "ACP initialize",
        );
        assert(!init.error, JSON.stringify(init));
        p.send({
          jsonrpc: "2.0",
          id: 2,
          method: "session/new",
          params: { cwd, mcpServers: [] },
        });
        const created = await waitFor(
          () => p.lines.find((x) => x.id === 2),
          "ACP session",
        );
        assert(created.result?.sessionId, JSON.stringify(created));
        p.send({
          jsonrpc: "2.0",
          id: 3,
          method: "session/prompt",
          params: {
            sessionId: created.result.sessionId,
            prompt: [{ type: "text", text: "ACP_INGRESS" }],
          },
        });
        const answer = await waitFor(
          () => p.lines.find((x) => x.id === 3),
          "ACP response",
        );
        assert(!answer.error, JSON.stringify(answer));
        await waitFor(
          () =>
            spans()
              .slice(start)
              .some((s) => !s.parentSpanId),
          "ACP root",
        );
        assert.equal(
          spans()
            .slice(start)
            .filter((s) => !s.parentSpanId).length,
          1,
        );
      }
      results.push({
        mode,
        status: "PASS",
        spans: spans().slice(start),
        events: p.lines.map((x) => ({
          type: x.type,
          id: x.id,
          method: x.method,
        })),
        stderr: p.stderr(),
      });
    } catch (e) {
      results.push({
        mode,
        status: "FAIL",
        error: e.message,
        lines: p.lines,
        stderr: p.stderr(),
      });
      throw e;
    } finally {
      await p.close();
      await fs.writeFile(
        path.join(base, "protocols.json"),
        JSON.stringify(results, null, 2),
      );
    }
  }
};
