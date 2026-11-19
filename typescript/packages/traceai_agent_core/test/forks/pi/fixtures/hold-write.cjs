// Disposable cloud-test child: pause a real protected write before atomic rename.
const fs = require("node:fs/promises");
const core = require(process.argv[3] || "../../../../dist/src/index.js");
const original = fs.rename;
const keepAlive = setInterval(() => {}, 1000);
fs.rename = async (...args) => {
  if (args[1] === process.argv[2]) {
    process.send({ type: "held" });
    await new Promise((resolve) => process.once("message", resolve));
  }
  return original(...args);
};
core
  .updateJson(process.argv[2], (old) => ({
    ...old,
    count: (old?.count || 0) + 1,
  }))
  .then(() => {
    process.send({ type: "done" });
    clearInterval(keepAlive);
    process.disconnect();
  })
  .catch((error) => {
    process.send({ type: "error", error: error.message });
    process.exit(1);
  });
