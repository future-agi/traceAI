const { requestUsage } = require("../index.cjs");
// Native hook subprocesses exercise the durable journal and inter-process locks.
const fs = require("node:fs/promises"),
  path = require("node:path");
const { packages, delay, run } = require("../index.cjs");
module.exports = {
  agent: "codex",
  async create(f) {
    const file = path.join(f.env.CODEX_HOME, "sessions/conformance.jsonl");
    await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
    await fs.writeFile(
      file,
      JSON.stringify({
        type: "session_meta",
        payload: { id: "conf-session" },
      }) + "\n",
      { mode: 0o600 },
    );
    const row = (type, payload) =>
      fs.appendFile(
        file,
        JSON.stringify({ timestamp: new Date().toISOString(), type, payload }) +
          "\n",
      );
    return {
      async turn(s) {
        const emit = async (name, data = {}) => {
          const result = await run(
            process.execPath,
            [packages + "/traceai_codex_plugin/dist/hook.js"],
            {
              env: f.env,
              cwd: f.project,
              input: JSON.stringify({
                hook_event_name: name,
                session_id: s.session,
                turn_id: s.id,
                cwd: f.project,
                transcript_path: file,
                ...data,
              }),
            },
          );
          if (result.code !== 0 || result.stdout)
            throw Error(
              "native hook violated fail-open output contract: " + name,
            );
        };
        await row("event_msg", { type: "task_started", turn_id: s.id });
        await emit("UserPromptSubmit", { prompt: `${s.input} ${s.secret}` });
        for (let i = 0; i < s.calls; i++) {
          await emit("PreModelRequest", {
            model: requestUsage(s, i).model,
            messages: [
              { role: "user", content: s.input },
              { role: "assistant", content: s.output },
              { role: "tool", content: s.tool },
            ],
          });
          await row("response_item", {
            type: "message",
            role: "assistant",
            content: [{ text: s.output }],
          });
          await emit("PostModelRequest", {
            model: requestUsage(s, i).model,
            status:
              s.outcome === "error" && i === s.calls - 1
                ? "failed"
                : "completed",
            error: s.outcome === "error" ? s.error : undefined,
            response: s.output,
            token_usage: { input_tokens: requestUsage(s, i).input, output_tokens: requestUsage(s, i).output },
          });
        }
        const tool = async (i) => {
          await emit("PreToolUse", {
            tool_use_id: "tool-" + i,
            tool_name: "read",
            tool_input: { path: s.input, password: "SYNTH_PASSWORD_19275" },
          });
          await emit("PostToolUse", {
            tool_use_id: "tool-" + i,
            tool_name: "read",
            tool_response: s.outcome === "error" ? { is_error: true, error: s.error } : s.tool,
          });
        };
        if (s.overlap)
          await Promise.all(Array.from({ length: s.tools }, (_, i) => tool(i)));
        else for (let i = 0; i < s.tools; i++) await tool(i);
        let late = Promise.resolve();
        if (s.outcome !== "incomplete") {
          const complete = () =>
            row("event_msg", {
              type: "task_complete",
              turn_id: s.id,
              last_agent_message: s.output,
            });
          late = s.lateMs ? delay(s.lateMs).then(complete) : complete();
          if (!s.lateMs) await late;
          await emit(s.outcome === "interrupted" ? "Interrupt" : "Stop", {
            last_assistant_message: s.output,
            error: s.error,
            status: s.outcome === "error" ? "failed" : "completed",
          });
        }
        await late;
        await emit("SessionEnd");
      },
    };
  },
};
