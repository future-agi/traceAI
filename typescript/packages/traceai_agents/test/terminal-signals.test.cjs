const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

// A terminal sends SIGINT to the foreground process group, not just the
// wrapper PID. This must deliver exactly one interrupt to the agent.
for (const interactive of [false, true])
  test(
    `foreground group interrupt is delivered once (${interactive ? "interactive" : "noninteractive"})`,
    { skip: process.platform === "win32" },
    async (t) => {
      const home = fs.mkdtempSync(path.join(os.tmpdir(), "fi-signal-"));
      const countFile = path.join(home, "interrupts");
      t.after(() => fs.rmSync(home, { recursive: true, force: true }));
      const agent = path.join(home, "agent.cjs");
      fs.writeFileSync(
        agent,
        `
    const fs = require('node:fs');
    let interrupts = 0;
    process.on('SIGINT', () => {
      fs.writeFileSync(${JSON.stringify(countFile)}, String(++interrupts));
      setTimeout(() => process.exit(0), 200).unref();
    });
    process.stdout.write('ready\\n');
    setInterval(() => {}, 1000);
  `,
      );
      const driver = path.join(home, "driver.cjs");
      fs.writeFileSync(
        driver,
        `
    // Simulate the TTY flag separately from group delivery; this test does
    // not require an actual controlling terminal in CI.
    if (${interactive}) Object.defineProperty(process.stdin, 'isTTY', { value: true });
    const { runAgent } = require(${JSON.stringify(path.resolve(__dirname, "../dist/src/cli/run.js"))});
    runAgent({ launch: async () => ({ executable: process.execPath,
      args: [${JSON.stringify(agent)}], env: {} }) }, {}, [], false)
      .then(({code, signal}) => { process.stdout.write('result:' + JSON.stringify({code, signal})); })
      .catch(() => process.exitCode = 97);
  `,
      );
      // A fresh process group models a foreground terminal without interrupting
      // the test runner itself. The spawned agent initially belongs to this group.
      const wrapper = spawn(process.execPath, [driver], {
        detached: true,
        stdio: ["ignore", "pipe", "pipe"],
        env: { HOME: home, PATH: process.env.PATH },
      });
      let stdout = "",
        stderr = "";
      const done = new Promise((resolve) =>
        wrapper.once("exit", (code, signal) => resolve({ code, signal })),
      );
      const ready = new Promise((resolve, reject) => {
        wrapper.stdout.on("data", (chunk) => {
          stdout += chunk;
          if (stdout.includes("ready\n")) resolve();
        });
        wrapper.once("exit", () =>
          reject(new Error("wrapper exited before agent ready")),
        );
      });
      wrapper.stderr.on("data", (chunk) => {
        stderr += chunk;
      });
      const timeout = setTimeout(() => {
        try {
          process.kill(-wrapper.pid, "SIGKILL");
        } catch {}
        wrapper.kill("SIGKILL");
      }, 5000);
      try {
        await ready;
        process.kill(-wrapper.pid, "SIGINT");
        const result = await done;
        assert.deepEqual(result, { code: 0, signal: null }, stderr);
        assert.equal(fs.readFileSync(countFile, "utf8"), "1", stdout + stderr);
        assert.match(stdout, /result:/);
      } finally {
        clearTimeout(timeout);
        wrapper.stdout.destroy();
        wrapper.stderr.destroy();
      }
    },
  );
