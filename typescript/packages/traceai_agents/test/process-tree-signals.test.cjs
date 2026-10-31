const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

test("interactive wrapper-only mode refuses a shared process group before launch", { skip: process.platform === "win32" }, async () => {
  const { runAgent } = require("../dist/src/cli/run.js");
  let launched = false;
  await assert.rejects(() => runAgent({ launch: async () => { launched = true; throw Error("must not launch"); } }, {}, [], false, { signalDelivery: "dedicated-wrapper-group" }), (error) => error.message === "UNSUPPORTED_SIGNAL_DELIVERY");
  assert.equal(launched, false);
});

// The agent launches a tool which remains alive after the agent handles SIGINT.
// Keep its process group for cleanup even if the wrapper exits prematurely.
for (const mode of ["noninteractive-group", "interactive-wrapper-only"]) {
  test(`interrupt reaches agent and tool (${mode})`, { skip: process.platform === "win32" }, async (t) => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), "fi-tree-signal-"));
    t.after(() => fs.rmSync(home, { recursive: true, force: true }));
    const tool = path.join(home, "tool.cjs");
    const agent = path.join(home, "agent.cjs");
    const driver = path.join(home, "driver.cjs");
    fs.writeFileSync(tool, `process.on('SIGINT',()=>{console.log('tool-interrupted');process.exit(0)});console.log('tool-ready');setInterval(()=>{},1000)`);
    fs.writeFileSync(agent, `const {spawn}=require('node:child_process');spawn(process.execPath,[${JSON.stringify(tool)}],{stdio:'inherit'});console.log('agent-pid:'+process.pid);process.on('SIGINT',()=>{console.log('agent-interrupted');setTimeout(()=>process.exit(0),100)});setInterval(()=>{},1000)`);
    fs.writeFileSync(driver, `if (${mode === "interactive-wrapper-only"}) Object.defineProperty(process.stdin,'isTTY',{value:true});const {runAgent}=require(${JSON.stringify(path.resolve(__dirname, "../dist/src/cli/run.js"))});runAgent({launch:async()=>({executable:process.execPath,args:[${JSON.stringify(agent)}],env:{}})},{},[],false,${JSON.stringify(mode === "interactive-wrapper-only" ? { signalDelivery: "dedicated-wrapper-group" } : {})}).then(x=>console.log('result:'+JSON.stringify(x))).catch(e=>{console.error(e);process.exitCode=97})`);
    const wrapper = spawn(process.execPath, [driver], { detached: true, stdio: ["ignore", "pipe", "pipe"], env: { HOME: home, PATH: process.env.PATH } });
    let output = "", stderr = "", agentPid = 0;
    wrapper.stdout.on("data", (chunk) => { output += chunk; const m = output.match(/agent-pid:(\d+)/); if (m) agentPid = Number(m[1]); });
    wrapper.stderr.on("data", (chunk) => { stderr += chunk; });
    const done = new Promise(resolve => wrapper.once("exit", (code, signal) => resolve({code, signal})));
    const ready = new Promise((resolve, reject) => {
      const check = () => { if (output.includes("tool-ready")) resolve(); };
      wrapper.stdout.on("data", check);
      check();
      wrapper.once("exit", () => reject(new Error("wrapper exited before tool ready: " + output + stderr)));
    });
    const timeout = setTimeout(() => { try { process.kill(-wrapper.pid, "SIGKILL"); } catch {} if (agentPid) try { process.kill(-agentPid, "SIGKILL"); } catch {} }, 5000);
    try {
      await ready;
      if (mode === "interactive-wrapper-only") wrapper.kill("SIGINT");
      else process.kill(-wrapper.pid, "SIGINT");
      const result = await done;
      assert.deepEqual(result, { code: 0, signal: null }, output + stderr);
      assert.match(output, /agent-interrupted/);
      assert.match(output, /tool-interrupted/);
      assert.match(output, /result:/);
    } finally {
      clearTimeout(timeout);
      if (agentPid) try { process.kill(-agentPid, "SIGKILL"); } catch {}
      try { process.kill(-wrapper.pid, "SIGKILL"); } catch {}
      wrapper.stdout.destroy(); wrapper.stderr.destroy();
    }
  });
}
