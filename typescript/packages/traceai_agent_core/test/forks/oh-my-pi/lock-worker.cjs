// Test helper, launched only in E2B. Intercepts a pending atomic write, not the lock implementation.
const fs = require("node:fs/promises");
const { updateJson, disable } = require("../../../dist/src/index.js");
const [mode, file, id] = process.argv.slice(2);
async function main() {
  if (mode === "hold") {
    const rename = fs.rename;
    fs.rename = async (...args) => {
      process.send("locked");
      await new Promise((resolve) => process.once("message", resolve));
      return rename(...args);
    };
    await updateJson(file, (old) => ({ ...old, held: true }));
    process.disconnect();
  } else if (mode === "write") {
    for (let i = 0; i < 8; i++) {
      await updateJson(file, (old) => ({ ...old, [id]: (old?.[id] ?? 0) + 1 }));
    }
    process.disconnect();
  } else if (mode === "disable") {
    await disable({ agent: id, home: file, env: {} });
    process.disconnect();
  }
}
main().catch((e) => {
  console.error(e);
  process.exit(1);
});
