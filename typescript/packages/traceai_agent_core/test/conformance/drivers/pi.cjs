const { requestUsage } = require("../index.cjs");
// Native extension callbacks from each package's extension.test.cjs; no tracer doubles.
const { packages, delay } = require("../index.cjs");
function driver(agent) {
  return {
    agent,
    group: "pi/oh-my-pi",
    async create(f) {
      const plugin = require(
        packages +
          `/traceai_${agent === "pi" ? "pi" : "oh_my_pi"}_plugin/dist/index.js`,
      ).default;
      const handlers = {},
        bus = {},
        entries = [];
      let session = "conf-session";
      const controller = new AbortController();
      const ctx = {
        cwd: f.project,
        mode: "json",
        hasUI: false,
        signal: controller.signal,
        model: { id: "conf-model", provider: "mock" },
        agent: { kind: "main", id: "Main" },
        sessionManager: {
          getSessionId: () => session,
          getBranch: () => entries,
        },
        ui: { setStatus() {}, notify() {} },
      };
      plugin({
        on(n, h) {
          handlers[n] = h;
          return () => delete handlers[n];
        },
        registerCommand() {},
        events: {
          on(n, h) {
            bus[n] = h;
            return () => delete bus[n];
          },
          emit(n, d) {
            return bus[n]?.(d);
          },
        },
      });
      const emit = async (type, data = {}) => {
        if (handlers[type]) return handlers[type]({ type, ...data }, ctx);
      };
      await emit("session_start");
      return {
        async turn(s) {
          session = s.session;
          entries.push({
            id: s.id,
            type: "message",
            message: { role: "user", content: `${s.input} ${s.secret}` },
          });
          await emit("before_agent_start", {
            reason: "prompt",
            prompt: `${s.input} ${s.secret}`,
          });
          await emit("agent_start");
          const tool = async (i) => {
            await emit("tool_execution_start", {
              toolCallId: `tool-${i}`,
              toolName: "read",
              args: { path: s.input, password: "SYNTH_PASSWORD_19275" },
            });
            await emit("tool_execution_end", {
              toolCallId: `tool-${i}`,
              toolName: "read",
              isError: s.outcome === "error",
              result: {
                content: [
                  {
                    type: "text",
                    text: s.outcome === "error" ? s.error : s.tool,
                  },
                ],
              },
            });
          };
          for (let i = 0; i < s.calls; i++) {
            if (i === 1 && s.outcome === "error" && s.tools) await tool(0);
            await emit("before_provider_request", {
              payload: {
                messages: [
                  { role: "user", content: s.input },
                  { role: "assistant", content: s.output },
                  {
                    role: "toolResult",
                    isError: s.outcome === "error" && i > 0,
                    toolCallId: "tool-0",
                    content: [
                      {
                        type: "text",
                        text: s.outcome === "error" && i > 0 ? s.error : s.tool,
                      },
                    ],
                  },
                ],
              },
            });
            await emit("message_end", {
              message: {
                role: "assistant",
                timestamp: Date.now() + i,
                completedAt: Date.now() + i + 1,
                model: requestUsage(s, i).model,
                provider: "mock",
                content: [{ type: "text", text: s.output }],
                stopReason:
                  i === s.calls - 1 && s.outcome === "error"
                    ? "error"
                    : i === s.calls - 1 && s.outcome === "interrupted"
                      ? "aborted"
                      : "stop",
                errorMessage: s.outcome === "error" ? s.error : undefined,
                usage: { input: requestUsage(s, i).input, output: requestUsage(s, i).output, cacheRead: 0, cacheWrite: 0 },
              },
            });
          }
          if (s.overlap)
            await Promise.all(
              Array.from({ length: s.tools }, (_, i) => tool(i)),
            );
          else
            for (
              let i = s.outcome === "error" && s.calls > 1 ? 1 : 0;
              i < s.tools;
              i++
            )
              await tool(i);
          if (s.lateMs) await delay(s.lateMs);
          if (s.outcome === "interrupted") controller.abort();
          if (s.outcome === "incomplete") await emit("session_shutdown");
          else {
            await emit("agent_end", { aborted: s.outcome === "interrupted" });
            if (agent === "pi")
              await emit("agent_settled", {
                aborted: s.outcome === "interrupted",
              });
          }
        },
        async flush() {
          await emit("session_shutdown");
        },
        async close() {
          await emit("session_shutdown");
        },
      };
    },
  };
}
module.exports = driver("pi");
module.exports.forAgent = driver;
