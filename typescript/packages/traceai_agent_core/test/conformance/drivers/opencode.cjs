const { requestUsage } = require("../index.cjs");
// V2 native observer events, from privacy-export.test.mjs and observer.test.mjs.
const { packages, delay } = require("../index.cjs");
module.exports = {
  agent: "opencode",
  group: "opencode-v2",
  async create(f) {
    const { Observer } = await import(
      packages + "/traceai_opencode_plugin/dist/observer.js"
    );
    const tracer = f.core.createTurnTracer({ config: f.config });
    const observer = new Observer(tracer, f.config, { directory: f.project });
    let seq = 0;
    return {
      async turn(s) {
        const event = (type, data = {}) =>
          observer.event({
            id: "e-" + ++seq,
            type,
            created: Date.now(),
            location: { directory: f.project },
            data: { sessionID: s.session, ...data },
          });
        event("session.inbox.enqueued", {
          inboxID: s.id,
          item: { type: "user", payload: { text: `${s.input} ${s.secret}` } },
        });
        event("session.inbox.delivered", { inboxID: s.id });
        for (let i = 0; i < s.calls; i++) {
          observer.context(
            s.session,
            [
              { role: "user", content: s.input },
              { role: "assistant", content: s.output },
              {
                role: "tool",
                content:
                  s.outcome === "error"
                    ? [
                        {
                          type: "tool-result",
                          id: "history-tool",
                          name: "read",
                          result: { type: "error", value: s.error },
                        },
                      ]
                    : s.tool,
              },
            ],
            [],
            "primary",
          );
          event("session.step.started", {
            assistantMessageID: `${s.id}-m${i}`,
            model: { id: requestUsage(s, i).model, providerID: "mock" },
            started: Date.now(),
          });
          event("session.text.ended", {
            assistantMessageID: `${s.id}-m${i}`,
            text: s.output,
          });
          event("session.step.ended", {
            assistantMessageID: `${s.id}-m${i}`,
            tokens: {
              input: requestUsage(s, i).input,
              output: requestUsage(s, i).output,
              reasoning: 0,
              cache: { read: 0, write: 0 },
            },
          });
        }
        const tool = (i) => {
          observer.toolBefore(s.session, "tool-" + i, "read", {
            path: s.input,
            password: "SYNTH_PASSWORD_19275",
          });
          observer.toolAfter(
            s.session,
            "tool-" + i,
            s.outcome === "error" ? s.error : s.tool,
            s.outcome === "error",
          );
        };
        if (s.overlap)
          await Promise.all(
            Array.from({ length: s.tools }, (_, i) =>
              Promise.resolve().then(() => tool(i)),
            ),
          );
        else for (let i = 0; i < s.tools; i++) tool(i);
        if (s.lateMs) await delay(s.lateMs);
        if (s.outcome === "incomplete") await observer.close();
        else
          event(
            "session.execution." +
              {
                success: "succeeded",
                error: "failed",
                interrupted: "interrupted",
              }[s.outcome],
            { reason: s.outcome === "interrupted" ? "user" : s.error },
          );
        await tracer.flush();
      },
      async flush() {
        await tracer.shutdown();
      },
      async close() {
        await observer.close();
      },
    };
  },
};
