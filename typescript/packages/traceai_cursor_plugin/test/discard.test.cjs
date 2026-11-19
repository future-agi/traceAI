const { test } = require("node:test");
const assert = require("node:assert/strict");
const { fixture, core, spans } = require("./helpers.cjs");
test("route discard destroys queued and active content without finalizing it to the old destination", async (t) => {
  const f = await fixture(t),
    tracer = core.createTurnTracer({ config: f.config });
  const turn = tracer.startTurn({
    sessionId: "s",
    turnId: "t",
    input: "private pending prompt",
  });
  turn
    .child({ id: "tool", kind: "tool", input: "pending file content" })
    .end({ output: "pending answer" });
  assert.equal(tracer.state.pending, 1);
  await tracer.discard();
  turn.end();
  await tracer.flush();
  assert.equal(tracer.state.pending, 0);
  assert.equal(spans(f.requests).length, 0);
});
