const { execFileSync } = require("node:child_process");
const { rmSync } = require("node:fs");
rmSync("dist", { recursive: true, force: true });
execFileSync(
  process.execPath,
  [require.resolve("typescript/bin/tsc"), "-p", "tsconfig.json"],
  { stdio: "inherit" },
);
require("esbuild").buildSync({
  entryPoints: ["src/hook.ts"],
  outfile: "dist/observer.cjs",
  bundle: true,
  platform: "node",
  format: "cjs",
  target: "node22",
  legalComments: "eof",
});
