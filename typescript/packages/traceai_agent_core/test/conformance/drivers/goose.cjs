const { requestUsage } = require("../index.cjs");
// Native SQLite response records and hooks, based on hooks.test.cjs/goose.test.cjs.
const fs = require("node:fs/promises"),
  path = require("node:path");
const { DatabaseSync } = require("node:sqlite");
const { packages, delay } = require("../index.cjs");
const { handleHook } = require(packages + "/traceai_goose/dist/hook.js");
module.exports = {
  agent: "goose",
  async create(f) {
    await f.core.updateJson(
      path.join(f.config.paths.configDir, "goose-plugin.json"),
      (old) => old ?? { schemaVersion: 1, userScope: true },
    );
    const file = path.join(f.env.GOOSE_PATH_ROOT, "data/sessions/sessions.db");
    await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
    const db = new DatabaseSync(file);
    await fs.chmod(file, 0o600);
    db.exec(
      "CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, message_id TEXT, role TEXT, content_json TEXT, metadata_json TEXT, created_timestamp INTEGER)",
    );
    const add = (s, id, role, parts, meta = {}) =>
      db
        .prepare(
          "INSERT INTO messages(session_id,message_id,role,content_json,metadata_json,created_timestamp) VALUES(?,?,?,?,?,?)",
        )
        .run(
          s.session,
          `${s.id}-${id}`,
          role,
          JSON.stringify(parts),
          JSON.stringify({ userVisible: true, ...meta }),
          Math.floor(Date.now() / 1000),
        );
    return {
      async turn(s) {
        const hook = (event, rest = {}) =>
          handleHook({
            event,
            session_id: s.session,
            working_dir: f.project,
            ...rest,
          });
        await hook("UserPromptSubmit", { message: `${s.input} ${s.secret}` });
        add(s, "prompt", "user", [
          { type: "text", text: `${s.input} ${s.secret}` },
        ]);
        const write = () => {
          for (let i = 0; i < s.calls; i++) {
            add(
              s,
              `model-${i}`,
              "assistant",
              [
                { type: "text", text: s.output },
                ...(i === 0
                  ? Array.from({ length: s.tools }, (_, n) => ({
                      type: "toolRequest",
                      id: "tool-" + n,
                      toolCall: {
                        status: "success",
                        value: {
                          name: "read",
                          arguments: {
                            path: s.input,
                            password: "SYNTH_PASSWORD_19275",
                          },
                        },
                      },
                    }))
                  : []),
              ],
              {
                inference: { provider: "mock", requestedModel: requestUsage(s, i).model },
                usage: { inputTokens: requestUsage(s, i).input, outputTokens: requestUsage(s, i).output },
              },
            );
            if (i === 0)
              for (let n = 0; n < s.tools; n++)
                add(s, `tool-${n}`, "user", [
                  {
                    type: "toolResponse",
                    id: "tool-" + n,
                    toolResult: {
                      status: "success",
                      value: {
                        isError: s.outcome === "error",
                        content: [
                          {
                            type: "text",
                            text: s.outcome === "error" ? s.error : s.tool,
                          },
                        ],
                      },
                    },
                  },
                ]);
          }
        };
        const tool = async (i) => {
          await hook("PreToolUse", { tool_call_id: "tool-" + i });
          await hook("PostToolUse", { tool_call_id: "tool-" + i });
        };
        if (s.overlap)
          await Promise.all(Array.from({ length: s.tools }, (_, i) => tool(i)));
        else for (let i = 0; i < s.tools; i++) await tool(i);
        if (!s.lateMs) write();
        if (s.outcome === "success" || s.outcome === "error")
          await hook("Stop");
        const late = s.lateMs ? delay(s.lateMs).then(write) : Promise.resolve();
        await hook("SessionEnd", {
          reason: s.outcome === "interrupted" ? "user_cancel" : "exit",
        });
        await late;
      },
      async close() {
        db.close();
      },
    };
  },
};
