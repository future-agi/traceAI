const { buildSync } = require("esbuild");
buildSync({
  entryPoints: ["src/hook.ts"],
  outfile: "dist/hook.cjs",
  bundle: true,
  mainFields: ["module", "main"],
  platform: "node",
  target: "node22",
  format: "cjs",
  sourcemap: false,
});
const { mkdirSync, writeFileSync, copyFileSync } = require("node:fs");
const { EVENTS } = require("./dist/events.js");
mkdirSync("plugin/hooks", { recursive: true });
writeFileSync(
  "plugin/hooks/hooks.json",
  JSON.stringify(
    {
      hooks: Object.fromEntries(
        EVENTS.map((e) => [
          e,
          [
            {
              hooks: [
                {
                  type: "command",
                  command: 'node "${PLUGIN_ROOT}/hook.cjs"',
                  timeout: ["SessionEnd", "Interrupt"].includes(e) ? 3 : 10,
                  ...(e === "Stop" ? { async: true } : {}),
                },
              ],
            },
          ],
        ]),
      ),
    },
    null,
    2,
  ),
);
copyFileSync("dist/hook.cjs", "plugin/hook.cjs");
