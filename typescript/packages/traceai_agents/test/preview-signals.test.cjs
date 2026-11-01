const { test } = require("node:test");
const assert = require("node:assert/strict");
const { runAgent } = require("../dist/src/cli/run.js");

test("opt-in wrapper-group refuses ambiguous terminal and wrapper-only SIGINT before launch", async () => {
  let launched = false;
  await assert.rejects(
    runAgent(
      {
        launch: async () => {
          launched = true;
          throw Error("must not launch");
        },
      },
      {},
      [],
      false,
      { signalDelivery: "dedicated-wrapper-group" },
    ),
    /UNSUPPORTED_SIGNAL_DELIVERY/,
  );
  assert.equal(launched, false);
});

test(
  "group-leading wrapper also refuses ambiguous signal mode",
  { skip: process.platform === "win32" },
  async () => {
    const { spawn } = require("node:child_process");
    const path = require("node:path");
    const driver = `const {runAgent}=require(${JSON.stringify(path.resolve(__dirname, "../dist/src/cli/run.js"))});runAgent({launch:async()=>{console.log('launched');return {executable:process.execPath,args:['-e','setInterval(()=>{},1000)'],env:{}}}},{},[],false,{signalDelivery:'dedicated-wrapper-group'}).catch(e=>{console.log('refused:'+e.message)})`;
    const wrapper = spawn(process.execPath, ["-e", driver], {
      detached: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let text = "";
    wrapper.stdout.on("data", (c) => (text += c));
    const killed = setTimeout(() => {
      try {
        process.kill(-wrapper.pid, "SIGKILL");
      } catch {}
    }, 2000);
    try {
      await new Promise((resolve) => wrapper.once("exit", resolve));
      assert.match(text, /refused:UNSUPPORTED_SIGNAL_DELIVERY/);
      assert.doesNotMatch(text, /launched/);
    } finally {
      clearTimeout(killed);
      try {
        process.kill(-wrapper.pid, "SIGKILL");
      } catch {}
      wrapper.stdout.destroy();
      wrapper.stderr.destroy();
    }
  },
);
