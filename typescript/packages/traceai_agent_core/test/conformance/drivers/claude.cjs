// Synthetic on-disk transcript + real hook boundary, based on hooks.test.cjs.
const fs = require("node:fs/promises"),
  path = require("node:path");
const { packages, delay } = require("../index.cjs");
const { handleHook } = require(packages + "/traceai_claude_code/dist/hook.js");
module.exports = {
  agent: "claude",
  async create(f) {
    const file = path.join(
      f.home,
      ".claude/projects/conformance/conf-session.jsonl",
    );
    await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
    await fs.writeFile(file, "", { mode: 0o600 });
    return {
      async turn(s) {
        const base = {
          session_id: s.session,
          prompt_id: s.id,
          cwd: f.project,
          transcript_path: file,
        };
        const hook = (name, data = {}) =>
          handleHook({ ...base, hook_event_name: name, ...data });
        const row = (type, message, extra = {}) => ({
          type,
          message,
          sessionId: s.session,
          promptId: s.id,
          uuid: require("node:crypto").randomUUID(),
          version: "2.1.318",
          timestamp: new Date().toISOString(),
          ...extra,
        });
        await hook("UserPromptSubmit");
        await fs.appendFile(
          file,
          JSON.stringify(
            row("user", { role: "user", content: `${s.input} ${s.secret}` }),
          ) + "\n",
        );
        const rows = [];
        for (let i = 0; i < s.calls; i++) {
          rows.push(
            row(
              "assistant",
              {
                id: `${s.id}-message-${i}`,
                role: "assistant",
                model: "conf-model",
                content: [
                  { type: "text", text: s.output },
                  ...(i === 0
                    ? Array.from({ length: s.tools }, (_, n) => ({
                        type: "tool_use",
                        id: `tool-${n}`,
                        name: "Read",
                        input: {
                          path: s.input,
                          password: "SYNTH_PASSWORD_19275",
                        },
                      }))
                    : []),
                ],
                stop_reason: i === s.calls - 1 ? "end_turn" : "tool_use",
                usage: { input_tokens: 10, output_tokens: 4 },
              },
              { requestRef: `${s.id}-request-${i}` },
            ),
          );
          if (i === 0)
            rows.push(
              row("user", {
                role: "user",
                content: Array.from({ length: s.tools }, (_, n) => ({
                  type: "tool_result",
                  tool_use_id: `tool-${n}`,
                  content: s.outcome === "error" ? s.error : s.tool,
                  is_error: s.outcome === "error",
                })),
              }),
            );
        }
        if (s.outcome === "interrupted")
          rows.push(
            row("user", {
              role: "user",
              content: "[Request interrupted by user]",
            }),
          );
        const tool = async (i) => {
          await hook("PreToolUse", { tool_use_id: `tool-${i}` });
          await hook("PostToolUse", { tool_use_id: `tool-${i}` });
        };
        if (s.overlap)
          await Promise.all(Array.from({ length: s.tools }, (_, i) => tool(i)));
        else for (let i = 0; i < s.tools; i++) await tool(i);
        const write = () =>
          fs.appendFile(file, rows.map(JSON.stringify).join("\n") + "\n");
        const late = s.lateMs ? delay(s.lateMs).then(write) : write();
        if (!s.lateMs) await late;
        await hook(
          s.outcome === "error"
            ? "StopFailure"
            : s.outcome === "success"
              ? "Stop"
              : "SessionEnd",
          {
            reason: s.outcome === "interrupted" ? "prompt_input_exit" : "other",
            last_assistant_message: s.output,
          },
        );
        await late;
        if (s.outcome === "success" || s.outcome === "error")
          await hook("SessionEnd");
      },
    };
  },
};
