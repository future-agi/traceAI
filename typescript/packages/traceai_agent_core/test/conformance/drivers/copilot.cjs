// Native console exporter records through the production mapper and HTTP delivery.
const { packages, delay } = require("../index.cjs");
const { NativeSpans } = require(packages + "/traceai_copilot/dist/index.js");
const { randomBytes } = require("node:crypto");
module.exports = {
  agent: "copilot",
  aggregateUsage: true,
  async create(f) {
    const mapper = new NativeSpans(f.config),
      delivery = new f.core.BoundedSpanProcessor(f.config);
    return {
      async turn(s) {
        const traceId = randomBytes(16).toString("hex"),
          root = randomBytes(8).toString("hex");
        let seq = 0;
        const send = (
          operation,
          attrs = {},
          isRoot = false,
          failed = false,
          events = [],
        ) => {
          const span = mapper.accept({
            type: "span",
            traceId,
            spanId: isRoot ? root : randomBytes(8).toString("hex"),
            parentSpanId: isRoot ? undefined : root,
            name: operation,
            kind: 0,
            startTime: [100 + seq++, 0],
            endTime: [200 + seq, 0],
            attributes: {
              "gen_ai.operation.name": operation,
              "gen_ai.conversation.id": s.session,
              "gen_ai.request.model": "conf-model",
              ...attrs,
            },
            status: {
              code: failed ? 2 : 1,
              ...(failed ? { message: s.error } : {}),
            },
            events,
            instrumentationScope: {
              name: "github.copilot",
              version: "1.0.106",
            },
          });
          if (!span) throw Error("native span rejected: " + operation);
          delivery.onEnd(span);
        };
        for (let i = 0; i < s.calls; i++)
          send(
            "chat",
            {
              "gen_ai.usage.input_tokens": 10,
              "gen_ai.usage.output_tokens": 4,
              "gen_ai.input.messages": JSON.stringify([
                {
                  role: "user",
                  parts: [{ type: "text", content: `${s.input} ${s.secret}` }],
                },
                {
                  role: "assistant",
                  parts: [{ type: "text", content: s.output }],
                },
                { role: "tool", parts: [{ type: "text", content: s.tool }] },
              ]),
              "gen_ai.output.messages": s.output,
            },
            false,
            s.outcome === "error" && i === s.calls - 1,
            s.outcome === "interrupted" && i === s.calls - 1
              ? [
                  {
                    name: "github.copilot.session.abort",
                    time: [199, 0],
                    attributes: {
                      "github.copilot.abort_reason": "user_initiated",
                    },
                  },
                ]
              : [],
          );
        const tool = () =>
          send("execute_tool", {
            "gen_ai.tool.name": "read",
            "gen_ai.tool.call.arguments": JSON.stringify({
              path: s.input,
              password: "SYNTH_PASSWORD_19275",
            }),
            "gen_ai.tool.call.result": s.tool,
          });
        if (s.overlap)
          await Promise.all(
            Array.from({ length: s.tools }, () => Promise.resolve().then(tool)),
          );
        else for (let i = 0; i < s.tools; i++) tool();
        if (s.lateMs) await delay(s.lateMs);
        if (s.outcome !== "incomplete")
          send(
            "invoke_agent",
            {
              "gen_ai.usage.input_tokens": s.inputTokens ?? s.calls * 10,
              "gen_ai.usage.output_tokens": s.outputTokens ?? s.calls * 4,
            },
            true,
            s.outcome === "error",
          );
        await delivery.forceFlush();
      },
      async flush() {
        await delivery.drain();
      },
      async close() {
        await delivery.shutdown();
      },
    };
  },
};
