// Test-only receipt proves that the real child sent its reply while the parent
// event loop was blocked. No production deadlines or storage work are replaced.
const fs = require("node:fs");
const path = require("node:path");
let root, operation, id;
process.on("message", (message) => {
  if (message.op === "init") root = message.args.root;
  operation = message.op;
  id = message.id;
});
const send = process.send.bind(process);
process.send = (message) => {
  const receipt = operation === "status" && message.id === id && message.ok;
  return send(message, (error) => {
    if (!error && receipt)
      fs.writeFileSync(path.join(path.dirname(root), "reply-sent"), "sent", {
        mode: 0o600,
      });
  });
};
require("../../dist/src/runtime/storage-child.js");
