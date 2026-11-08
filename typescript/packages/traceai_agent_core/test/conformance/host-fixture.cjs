const fs = require("node:fs/promises"),
  path = require("node:path");
const { packages } = require("./index.cjs");
/** Native CLI stand-ins permit lifecycle testing without installing/logging in to hosts. */
async function prepareHost(f) {
  const bin = path.join(f.home, "bin");
  await fs.mkdir(bin, { mode: 0o700 });
  const script =
    "#!" +
    process.execPath +
    "\n" +
    (await fs.readFile(path.join(__dirname, "native-host.cjs"), "utf8"));
  const commands = {
    claude: "claude",
    codex: "codex",
    pi: "pi",
    "oh-my-pi": "omp",
    opencode: "opencode",
    "opencode-v1": "opencode",
    antigravity: "agy",
    copilot: "copilot",
    goose: "goose",
    hermes: "hermes",
    openclaw: "openclaw",
    grok: "grok",
    cursor: "cursor",
    "cursor-cli": "cursor-agent",
  };
  for (const command of [
    commands[f.agent],
    ...(f.agent === "grok" ? ["npm"] : []),
  ])
    await fs.writeFile(path.join(bin, command), script, { mode: 0o700 });
  const spec = {
    agent: f.agent,
    home: f.home,
    userDir: path.dirname(f.config.paths.user),
    codexEvents: require(packages + "/traceai_codex_plugin/dist/events.js")
      .EVENTS,
    codexVersion: require(packages + "/traceai_codex_plugin/package.json").version,
    hermesSource: path.resolve(
      packages,
      "../../python/frameworks/hermes-agent/hermes-plugin/futureagi",
    ),
    grokSource: path.join(packages, "traceai_grok_plugin"),
  };
  await fs.writeFile(path.join(bin, "fixture.json"), JSON.stringify(spec), {
    mode: 0o600,
  });
  f.env.PATH = bin + path.delimiter + process.env.PATH;
  process.env.PATH = f.env.PATH;
  // Seed an unrelated setting in the very file the adapter may modify.
  const filename = {
    claude: "settings.json",
    pi: "settings.json",
    "oh-my-pi": "settings.json",
    opencode: "opencode.json",
    "opencode-v1": "opencode.json",
    openclaw: "openclaw.json",
    cursor: "hooks.json",
    "cursor-cli": "hooks.json",
  }[f.agent];
  if (filename) {
    f.foreignSettings = path.join(spec.userDir, filename);
    await fs.mkdir(spec.userDir, { recursive: true, mode: 0o700 });
    await fs.writeFile(
      f.foreignSettings,
      JSON.stringify({
        conformanceForeignSetting: "must-survive",
        ...(["cursor", "cursor-cli"].includes(f.agent)
          ? { version: 1, hooks: {} }
          : {}),
        ...(f.agent === "openclaw"
          ? {
              plugins: {
                allow: ["other", "diagnostics-otel", "futureagi-openclaw"],
              },
            }
          : {}),
      }),
      { mode: 0o600 },
    );
  }
}
module.exports = { prepareHost };
