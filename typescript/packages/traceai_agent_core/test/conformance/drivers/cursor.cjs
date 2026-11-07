// Real durable hook journal + bounded finisher, not direct observer replay.
// CLI uses Cursor's shared hooks; its separate public setup gate is tested as-is.
const fs = require("node:fs/promises"),
  path = require("node:path");
const { packages, delay, spans } = require("../index.cjs");
const { normalizeEvent } = require(
  packages + "/traceai_cursor_plugin/dist/index.js",
);
const { appendEvent } = require(
  packages + "/traceai_cursor_plugin/dist/state.js",
);
const { processPending } = require(
  packages + "/traceai_cursor_plugin/dist/runtime.js",
);
function driver(agent) {
  return {
    agent,
    group: "cursor",
    aggregateUsage: true,
    async create(f) {
      const file = path.join(
        f.home,
        ".cursor/projects/conformance/agent-transcripts/conf-session/conf-session.jsonl",
      );
      await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
      await fs.writeFile(file, "", { mode: 0o600 });
      return {
        async turn(s) {
          const before = spans(f.batches).filter((x) => !x.parentSpanId).length;
          const emit = async (name, data = {}) => {
            const e = normalizeEvent(
              {
                cursor_version:
                  agent === "cursor-cli" ? "2026.11.05-86047df" : "3.24.18",
                conversation_id: s.session,
                generation_id: s.id,
                hook_event_name: name,
                transcript_path: s.aggregate ? undefined : file,
                ...data,
              },
              name,
              f.config,
            );
            if (!e) throw Error("native hook rejected: " + name);
            await appendEvent(f.config, e, {
              defer: true,
              projectDir: f.project,
            });
            if (name === "stop" || name === "sessionEnd")
              await processPending(f.config);
          };
          await emit("beforeSubmitPrompt", {
            prompt: `${s.input} ${s.secret}`,
          });
          const tools = async (i) => {
            await emit("preToolUse", {
              tool_name: "read",
              tool_use_id: "tool-" + i,
              tool_input: { path: s.input, password: "SYNTH_PASSWORD_19275" },
            });
            await emit(
              s.outcome === "error" ? "postToolUseFailure" : "postToolUse",
              {
                tool_name: "read",
                tool_use_id: "tool-" + i,
                tool_output: s.outcome === "error" ? s.error : s.tool,
                error_message: s.outcome === "error" ? s.error : undefined,
              },
            );
          };
          if (s.overlap)
            await Promise.all(
              Array.from({ length: s.tools }, (_, i) => tools(i)),
            );
          else for (let i = 0; i < s.tools; i++) await tools(i);
          const rows = [
            {
              role: "user",
              message: {
                content: [
                  {
                    type: "text",
                    text: `<user_query> ${s.input} ${s.secret} </user_query>`,
                  },
                ],
              },
            },
            ...Array.from({ length: s.calls }, (_, i) => ({
              role: "assistant",
              message: {
                id: `${s.id}-model-${i}`,
                model: "conf-model",
                content: [{ type: "text", text: s.output }],
              },
            })),
            { type: "turn_ended", status: s.outcome },
          ];
          const write = () =>
            fs.appendFile(file, rows.map(JSON.stringify).join("\n") + "\n");
          const late = s.lateMs ? delay(s.lateMs).then(write) : write();
          if (!s.lateMs) await late;
          if (!s.aggregate)
            await emit("afterAgentResponse", { text: s.output });
          await emit(s.outcome === "incomplete" ? "sessionEnd" : "stop", {
            reason: "other",
            status: {
              success: "completed",
              interrupted: "aborted",
              error: "error",
            }[s.outcome],
            input_tokens: s.inputTokens ?? s.calls * 10,
            output_tokens: s.outputTokens ?? s.calls * 4,
            model: "conf-model",
          });
          await late;
          if (f.core.collectionEnabled(f.config)) {
            const deadline = Date.now() + 31000;
            while (
              spans(f.batches).filter((x) => !x.parentSpanId).length ===
                before &&
              Date.now() < deadline
            )
              await delay(50);
          }
        },
      };
    },
  };
}
module.exports = driver("cursor");
module.exports.forAgent = driver;
