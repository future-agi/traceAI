// Native named hooks and PLANNER_RESPONSE transcript records from transcript.test.cjs.
const fs = require("node:fs/promises"),
  path = require("node:path");
const { packages, delay } = require("../index.cjs");
const { handleEvent } = require(
  packages + "/traceai_antigravity_plugin/dist/runtime.js",
);
module.exports = {
  agent: "antigravity",
  async create(f) {
    const file = path.join(
      f.home,
      ".gemini/antigravity-cli/brain/conf-session/.system_generated/logs/transcript_full.jsonl",
    );
    await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
    await fs.writeFile(file, "", { mode: 0o600 });
    let step = 0;
    return {
      async turn(s) {
        const first = step++,
          base = {
            conversationId: s.session,
            initialNumSteps: step,
            invocationNum: 0,
            transcriptPath: file,
            workspacePaths: [f.project],
            modelName: "conf-model",
            lastUserInput: `${s.input} ${s.secret}`,
          };
        const send = (event, extra = {}) =>
          handleEvent(event, { ...base, ...extra }, f.options);
        await fs.appendFile(
          file,
          JSON.stringify({
            step_index: first,
            source: "USER_EXPLICIT",
            type: "USER_INPUT",
            status: "DONE",
            content: `<USER_REQUEST>\n${s.input} ${s.secret}\n</USER_REQUEST>`,
          }) + "\n",
        );
        await send("PreInvocation");
        const records = [];
        for (let i = 0; i < s.calls; i++) {
          records.push({
            step_index: step++,
            source: "MODEL",
            type: "PLANNER_RESPONSE",
            status: "DONE",
            created_at: new Date().toISOString(),
            input_tokens: 10,
            output_tokens: 4,
            content: s.output,
            ...(i < s.tools
              ? {
                  tool_calls: [
                    {
                      name: "read",
                      args: { path: s.input, password: "SYNTH_PASSWORD_19275" },
                    },
                  ],
                }
              : {}),
          });
          if (i < s.tools)
            records.push({
              step_index: step++,
              source: "MODEL",
              type: "GENERIC",
              status: s.outcome === "error" ? "ERROR" : "DONE",
              error: s.outcome === "error" ? s.error : undefined,
              content: s.outcome === "error" ? s.error : s.tool,
            });
        }
        const write = () =>
          fs.appendFile(file, records.map(JSON.stringify).join("\n") + "\n");
        const late = s.lateMs ? delay(s.lateMs).then(write) : write();
        if (!s.lateMs) await late;
        const tool = (i) =>
          send("PostToolUse", {
            stepIdx: first + 2 + 2 * i,
            error: s.outcome === "error" ? s.error : undefined,
            toolCall: {
              name: "read",
              args: { path: s.input, password: "SYNTH_PASSWORD_19275" },
            },
          });
        if (s.overlap)
          await Promise.all(Array.from({ length: s.tools }, (_, i) => tool(i)));
        else for (let i = 0; i < s.tools; i++) await tool(i);
        await send("PostInvocation", { modelOutput: s.output });
        await send("Stop", {
          fullyIdle: true,
          terminationReason: {
            success: "NO_TOOL_CALL",
            error: "ERROR",
            interrupted: "USER_CANCELED",
            incomplete: "max_steps_exceeded",
          }[s.outcome],
        });
        await late;
      },
    };
  },
};
