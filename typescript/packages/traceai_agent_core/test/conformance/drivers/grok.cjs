// Production durable capture + ACP updates.jsonl from the qualified native schema.
const fs = require("node:fs/promises"),
  path = require("node:path");
const { packages, delay } = require("../index.cjs");
const { normalizeHook } = require(
  packages + "/traceai_grok_plugin/dist/index.js",
);
const { capture } = require(packages + "/traceai_grok_plugin/dist/capture.js");
module.exports = {
  agent: "grok",
  aggregateUsage: true,
  incompleteUsageUnavailable: true,
  async create(f) {
    const file = path.join(f.home, ".grok/sessions/conf-session/updates.jsonl");
    await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
    await fs.writeFile(file, "", { mode: 0o600 });
    let seq = 0;
    return {
      async turn(s) {
        const event = (name, rest = {}) =>
          capture(
            normalizeHook(
              {
                hookEventName: name,
                sessionId: s.session,
                promptId: s.id,
                timestamp: new Date().toISOString(),
                cwd: f.project,
                transcriptPath: file,
                ...rest,
              },
              f.config,
            ),
            f.config,
          );
        const row = (update, meta = {}) => ({
          timestamp: Math.floor(Date.now() / 1000),
          method: "session/update",
          params: {
            sessionId: s.session,
            update,
            _meta: {
              eventId: `${s.id}-${++seq}`,
              agentTimestampMs: Date.now(),
              promptId: s.id,
              ...meta,
            },
          },
        });
        await event("UserPromptSubmit", { prompt: `${s.input} ${s.secret}` });
        const streamBase = Date.now();
        const rows = [
          row({
            sessionUpdate: "hook_execution",
            event_name: "user_prompt_submit",
            prompt_id: s.id,
          }),
          row({
            sessionUpdate: "user_message_chunk",
            content: { type: "text", text: `${s.input} ${s.secret}` },
            _meta: { modelId: "conf-model" },
          }),
        ];
        for (let i = 0; i < s.calls; i++)
          rows.push(
            row(
              {
                sessionUpdate: "agent_message_chunk",
                content: { type: "text", text: s.output },
              },
              {
                streamStartMs: streamBase + i * 100,
                chunkId: 1,
                updateType: "AgentMessageChunk",
              },
            ),
          );
        for (let i = 0; i < s.tools; i++) {
          rows.push(
            row(
              {
                sessionUpdate: "tool_call",
                toolCallId: "tool-" + i,
                title: "read",
                rawInput: { path: s.input, password: "SYNTH_PASSWORD_19275" },
              },
              { streamStartMs: streamBase + i * 100, updateType: "ToolCall" },
            ),
          );
          rows.push(
            row({
              sessionUpdate: "tool_call_update",
              toolCallId: "tool-" + i,
              status: s.outcome === "error" ? "failed" : "completed",
              rawOutput: s.outcome === "error" ? { error: s.error } : s.tool,
            }),
          );
        }
        await fs.appendFile(file, rows.map(JSON.stringify).join("\n") + "\n");
        const tool = (i) =>
          event("PostToolUse", {
            toolUseId: "tool-" + i,
            toolName: "read",
            toolInput: { path: s.input },
            toolResult: s.tool,
            durationMs: 1,
          });
        if (s.overlap)
          await Promise.all(Array.from({ length: s.tools }, (_, i) => tool(i)));
        else for (let i = 0; i < s.tools; i++) await tool(i);
        const complete = () =>
          fs.appendFile(
            file,
            JSON.stringify(
              row({
                sessionUpdate: "turn_completed",
                prompt_id: s.id,
                stop_reason: {
                  success: "end_turn",
                  error: "error",
                  interrupted: "cancelled",
                }[s.outcome],
                usage: {
                  inputTokens: s.inputTokens ?? s.calls * 10,
                  outputTokens: s.outputTokens ?? s.calls * 4,
                  modelCalls: s.calls,
                  modelUsage: {
                    "conf-model": {
                      inputTokens: s.inputTokens ?? s.calls * 10,
                      outputTokens: s.outputTokens ?? s.calls * 4,
                      modelCalls: s.calls,
                    },
                  },
                },
              }),
            ) + "\n",
          );
        let late = Promise.resolve();
        if (s.outcome !== "incomplete") {
          late = s.lateMs ? delay(s.lateMs).then(complete) : complete();
          if (!s.lateMs) await late;
          await event(
            {
              success: "Stop",
              error: "StopFailure",
              interrupted: "StopCancelled",
            }[s.outcome],
            { errorDetails: s.error },
          );
        }
        await event("SessionEnd");
        await late;
      },
    };
  },
};
