const { requestUsage } = require("../index.cjs");
// V1 event/hook order derived from fixtures/opencode-1.19.0-session.json.
const { packages, delay } = require("../index.cjs");
module.exports = {
  agent: "opencode-v1",
  async create(f) {
    const { observe } = await import(
      packages + "/traceai_opencode_v1_plugin/dist/observer.js"
    );
    const tracer = f.core.createTurnTracer({ config: f.config });
    const hooks = observe(f.config, tracer);
    return {
      async turn(s) {
        const event = (type, properties = {}) =>
          hooks.event({
            event: {
              type,
              properties: { sessionID: s.session, ...properties },
            },
          });
        await event("session.created", {
          info: {
            id: s.session,
            directory: f.project,
            time: { created: Date.now() },
          },
        });
        await hooks["chat.message"](
          {
            sessionID: s.session,
            agent: "build",
            model: { modelID: "conf-model", providerID: "mock" },
          },
          {
            message: {
              id: s.id,
              role: "user",
              sessionID: s.session,
              time: { created: Date.now() },
            },
            parts: [{ type: "text", text: `${s.input} ${s.secret}` }],
          },
        );
        for (let i = 0; i < s.calls; i++) {
          const messageID = `${s.id}-m${i}`,
            time = Date.now();
          const info = {
            id: messageID,
            parentID: s.id,
            role: "assistant",
            sessionID: s.session,
            modelID: requestUsage(s, i).model,
            providerID: "mock",
            time: { created: time },
          };
          await event("message.updated", { info });
          await event("message.part.updated", {
            part: {
              id: messageID + "-start",
              messageID,
              sessionID: s.session,
              type: "step-start",
            },
          });
          await hooks["experimental.chat.messages.transform"](
            {},
            {
              messages: [
                {
                  info: { role: "user", sessionID: s.session },
                  parts: [{ type: "text", text: s.input }],
                },
                {
                  info: { role: "assistant", sessionID: s.session },
                  parts: [
                    { type: "text", text: s.output },
                    {
                      type: "tool",
                      tool: "read",
                      state: {
                        status: "completed",
                        input: { path: s.input },
                        output: s.tool,
                      },
                    },
                  ],
                },
              ],
            },
          );
          await event("message.part.updated", {
            part: {
              id: messageID + "-text",
              messageID,
              sessionID: s.session,
              type: "text",
              text: s.output,
            },
          });
          if (i < s.tools)
            await event("message.part.updated", {
              part: {
                id: messageID + "-tool",
                messageID,
                sessionID: s.session,
                type: "tool",
                tool: "read",
                callID: "tool-" + i,
                state: {
                  status: s.outcome === "error" ? "error" : "completed",
                  error: s.outcome === "error" ? s.error : undefined,
                  input: { path: s.input, password: "SYNTH_PASSWORD_19275" },
                  output: s.outcome === "error" ? s.error : s.tool,
                  time: { start: time, end: time + 1 },
                },
              },
            });
          await event("message.updated", {
            info: {
              ...info,
              time: { created: time, completed: time + 2 },
              finish: "stop",
              tokens: {
                input: requestUsage(s, i).input,
                output: requestUsage(s, i).output,
                reasoning: 0,
                cache: { read: 0, write: 0 },
              },
            },
          });
        }
        if (s.overlap)
          await Promise.all(
            Array.from({ length: s.tools }, (_, i) =>
              event("message.part.updated", {
                part: {
                  id: `${s.id}-m${i}-tool`,
                  messageID: `${s.id}-m${i}`,
                  sessionID: s.session,
                  type: "tool",
                  tool: "read",
                  callID: "tool-" + i,
                  state: {
                    status: "completed",
                    input: { path: s.input },
                    output: s.tool,
                    time: { start: Date.now(), end: Date.now() },
                  },
                },
              }),
            ),
          );
        if (s.lateMs) await delay(s.lateMs);
        if (s.outcome === "incomplete") await hooks.dispose();
        else {
          if (s.outcome !== "success")
            await event("session.error", {
              error: {
                name:
                  s.outcome === "interrupted"
                    ? "MessageAbortedError"
                    : "APIError",
                data: { message: s.error },
              },
            });
          await event("session.idle");
        }
        await tracer.flush();
      },
      async flush() {
        await hooks.dispose();
      },
      async close() {
        await hooks.dispose();
      },
    };
  },
};
