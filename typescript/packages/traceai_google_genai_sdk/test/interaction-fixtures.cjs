const path = require("node:path");
const { createRequire } = require("node:module");
const req = process.env.CONSUMER_DIR
  ? createRequire(path.join(process.env.CONSUMER_DIR, "package.json"))
  : require;
// The packed SDKs' published declarations use steps/step.delta in 2.x;
// the 1.33 and 1.44 compatibility checkpoints use outputs/content.delta.
const { version } = req(
  path.resolve(req.resolve("@google/genai"), "../../../package.json"),
);
const modern = Number(version.split(".")[0]) >= 2;
exports.modern = modern;
exports.output = (text) =>
  modern
    ? { steps: [{ type: "model_output", content: [{ type: "text", text }] }] }
    : { outputs: [{ type: "text", text }] };
exports.eventType = (type) =>
  modern
    ? ({
        "interaction.start": "interaction.created",
        "interaction.complete": "interaction.completed",
        "content.delta": "step.delta",
      }[type] ?? type)
    : type;
