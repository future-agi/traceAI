// Production native capture + invocation-owned launcher delivery (prompt.test.cjs).
const { packages, delay } = require("../index.cjs");
const { createLauncherDelivery, LauncherCaptureProcessor } = require(
  packages + "/traceai_openclaw/dist/launcher-delivery.js",
);
const { randomBytes } = require("node:crypto");
module.exports = {
  agent: "openclaw",
  aggregateUsage: true,
  async create(f) {
    return {
      async turn(s) {
        const delivery = await createLauncherDelivery(f.config, s.session),
          capture = new LauncherCaptureProcessor(
            f.config,
            delivery.fd,
            Buffer.from(delivery.key, "hex"),
            s.session,
          );
        const traceId = randomBytes(16).toString("hex"),
          rootId = randomBytes(8).toString("hex");
        let seq = 0;
        const send = (name, attributes, isRoot = false, failed = false) => {
          const id = isRoot ? rootId : (++seq).toString(16).padStart(16, "0");
          capture.onEnd({
            name,
            spanContext: () => ({ traceId, spanId: id, traceFlags: 1 }),
            parentSpanContext: isRoot
              ? undefined
              : { traceId, spanId: rootId, traceFlags: 1 },
            kind: 0,
            startTime: [100, 0],
            endTime: [101, 0],
            duration: [1, 0],
            ended: true,
            status: {
              code: failed ? 2 : 1,
              ...(failed ? { message: s.error } : {}),
            },
            attributes,
            events: [],
            links: [],
            instrumentationScope: { name: "openclaw", version: "2026.10.5" },
          });
        };
        for (let i = 0; i < s.calls; i++)
          send(
            "openclaw.model.call",
            {
              "openclaw.model_call.observation_unit": "request",
              "gen_ai.request.model": "conf-model",
              "gen_ai.usage.input_tokens": 10,
              "gen_ai.usage.output_tokens": 4,
              "openclaw.content.input_messages": JSON.stringify([
                { role: "user", content: `${s.input} ${s.secret}` },
                { role: "assistant", content: s.output },
                {
                  role: "tool",
                  isError: s.outcome === "error",
                  content: s.outcome === "error" ? s.error : s.tool,
                },
              ]),
              "openclaw.content.output_messages": s.output,
            },
            false,
            s.outcome === "error" && i === s.calls - 1,
          );
        if (s.aggregate)
          send("openclaw.model.call", {
            "openclaw.model_call.observation_unit": "turn",
            "gen_ai.usage.input_tokens": s.inputTokens,
            "gen_ai.usage.output_tokens": s.outputTokens,
          });
        const tool = () =>
          send("openclaw.tool.execution", {
            "gen_ai.tool.name": "read",
            "openclaw.content.tool_input": JSON.stringify({
              path: s.input,
              password: "SYNTH_PASSWORD_19275",
            }),
            "openclaw.content.tool_output": s.tool,
          });
        if (s.overlap)
          await Promise.all(
            Array.from({ length: s.tools }, () => Promise.resolve().then(tool)),
          );
        else for (let i = 0; i < s.tools; i++) tool();
        send("openclaw.harness.run", {}, true);
        if (s.lateMs) await delay(s.lateMs);
        await delivery.finish(
          { success: 0, error: 1, interrupted: 130, incomplete: 137 }[
            s.outcome
          ],
        );
      },
    };
  },
};
