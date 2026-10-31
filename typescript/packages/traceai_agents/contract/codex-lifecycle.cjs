"use strict";
// Synthetic RPC driver; every Codex process stays inside the caller's loopback sandbox.
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const assert = require("node:assert/strict");
async function lifecycle({ binary, sandbox, home, env, scenario, calls }) {
  const app = spawn(
    "/usr/bin/sandbox-exec",
    ["-f", sandbox, binary, "app-server"],
    {
      cwd: home,
      env,
      stdio: ["pipe", "pipe", "pipe"],
    },
  );
  const pending = new Map(),
    notifications = [];
  let serial = 1,
    buffer = "",
    bytes = 0;
  const deadline = Date.now() + 45000;
  const killer = setTimeout(() => app.kill("SIGKILL"), 45000);
  app.stderr.resume();
  app.stdout.on("data", (chunk) => {
    bytes += chunk.length;
    if (bytes > 4 * 1024 * 1024) {
      app.kill("SIGKILL");
      return;
    }
    buffer += chunk;
    for (;;) {
      const n = buffer.indexOf("\n");
      if (n < 0) break;
      const m = JSON.parse(buffer.slice(0, n));
      buffer = buffer.slice(n + 1);
      if (pending.has(m.id)) {
        pending.get(m.id)(m);
        pending.delete(m.id);
      } else if (m.method) notifications.push(m);
    }
  });
  async function rpc(method, params) {
    const id = serial++;
    const result = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(Error("RPC_TIMEOUT " + method));
      }, 10000);
      pending.set(id, (m) => {
        clearTimeout(timer);
        m.error
          ? reject(Error("RPC_ERROR " + method + " " + m.error.code))
          : resolve(m.result);
      });
    });
    app.stdin.write(JSON.stringify({ id, method, params }) + "\n");
    return result;
  }
  async function wait(predicate) {
    while (!predicate()) {
      if (Date.now() >= deadline || app.exitCode !== null)
        throw Error("LIFECYCLE_TIMEOUT " + scenario);
      await new Promise((r) => setTimeout(r, 25));
    }
  }
  try {
    await rpc("initialize", {
      clientInfo: { name: "traceai-contract", version: "0.1.0" },
      capabilities: { experimentalApi: true },
    });
    app.stdin.write('{"method":"initialized"}\n');
    const { thread } = await rpc("thread/start", {
      cwd: home,
      sandbox: "read-only",
      approvalPolicy: "never",
      historyMode: "paginated",
    });
    const { turn } = await rpc("turn/start", {
      threadId: thread.id,
      input: [
        {
          type: "text",
          text: "Synthetic lifecycle fixture. Reply fixture-ok.",
          text_elements: [],
        },
      ],
    });
    if (scenario === "interrupted") {
      await wait(() => calls() >= 1);
      await rpc("turn/interrupt", { threadId: thread.id, turnId: turn.id });
    }
    await wait(() =>
      notifications.some(
        (n) => n.method === "turn/completed" && n.params.turn.id === turn.id,
      ),
    );
    if (scenario === "compaction") {
      const marker = notifications.length;
      await rpc("thread/compact/start", { threadId: thread.id });
      await wait(() =>
        notifications
          .slice(marker)
          .some(
            (n) =>
              n.method === "item/completed" &&
              n.params.item.type === "contextCompaction",
          ),
      );
      await wait(() =>
        notifications.slice(marker).some((n) => n.method === "turn/completed"),
      );
    }
    if (scenario === "subagent") {
      await wait(() => calls() >= 3);
      // Parent and child completion are observed independently, not timed together.
      await wait(
        () =>
          notifications.filter((n) => n.method === "turn/completed").length >=
          2,
      );
    }
    assert.ok(notifications.some((n) => n.method === "turn/completed"));
    return "fixture-ok";
  } finally {
    clearTimeout(killer);
    if (app.exitCode === null) {
      app.kill("SIGTERM");
      await once(app, "exit");
    }
  }
}
module.exports = { lifecycle };
