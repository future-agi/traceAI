// Separate process exercises the real protected writer, not a fabricated lock file.
const fs = require("node:fs/promises");
const { updateJson } = require("../../../../dist/src/index.js");
const [mode, target, key] = process.argv.slice(2);
if (mode === "hold") {
  const rename = fs.rename.bind(fs);
  fs.rename = async (from, to) => {
    if (to === target) {
      process.send({ locked: true });
      await new Promise((resolve) => process.once("message", resolve));
    }
    return rename(from, to);
  };
}
(async () => {
  for (let i = 0; i < (mode === "increment" ? 5 : 1); i++)
    await updateJson(target, (old) => ({
      ...old,
      [key]: (old?.[key] ?? 0) + 1,
    }));
  process.disconnect();
})().catch(() => process.exit(1));
