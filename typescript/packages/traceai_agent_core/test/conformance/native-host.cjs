// Deterministic native CLI fixture; runs only under the suite's throwaway HOME.
// It simulates native install/list APIs, never CoreAdapter or Future AGI responses.
const fs = require("node:fs"),
  path = require("node:path");
const agent = path.basename(process.argv[1]),
  args = process.argv.slice(2).filter((x) => x !== "--no-auto-update");
const spec = JSON.parse(
  fs.readFileSync(
    path.join(path.dirname(process.argv[1]), "fixture.json"),
    "utf8",
  ),
);
const root = path.join(spec.home, "native-fixture");
fs.mkdirSync(root, { recursive: true, mode: 0o700 });
const statePath = path.join(root, agent + ".json");
let state = fs.existsSync(statePath)
  ? JSON.parse(fs.readFileSync(statePath, "utf8"))
  : {};
const save = () =>
  fs.writeFileSync(statePath, JSON.stringify(state), { mode: 0o600 });
const output = (value) =>
  console.log(typeof value === "string" ? value : JSON.stringify(value));
const write = (file, value) => {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  fs.writeFileSync(file, JSON.stringify(value), { mode: 0o600 });
};
const versions = {
  claude: "2.1.318 (Claude Code)",
  codex: "codex-cli 0.167.0",
  pi: "1.5.0",
  omp: "omp/18.17.0",
  opencode: spec.agent === "opencode-v1" ? "1.19.0" : "2.2.5",
  agy: "1.5.2",
  copilot: "GitHub Copilot CLI 1.0.106.",
  goose: "goose 1.54.0",
  hermes: "Hermes Agent 2026.10.5",
  openclaw: "OpenClaw 2026.10.5 (fixture)",
  grok: "grok 1.0.77",
  agent: "2026.11.05-86047df",
  "cursor-agent": "2026.11.05-86047df",
};
if (args.includes("--version")) {
  output(versions[agent] || "1.0.0");
} else if (args.includes("--help"))
  output("--enable --json Start the Cursor Agent --output-format");
else if (agent === "copilot" || agent === "claude" || agent === "goose") {
  /* Detection is the only host call; adapter edits native hooks itself. */
} else if (agent === "pi") {
  const settings = path.join(process.env.PI_CODING_AGENT_DIR, "settings.json");
  if (args[0] === "install") {
    const data = fs.existsSync(settings)
      ? JSON.parse(fs.readFileSync(settings))
      : {};
    data.packages = [...(data.packages || []), args[1]];
    write(settings, data);
    write(
      path.join(
        path.dirname(settings),
        "npm/node_modules/@traceai/pi-plugin/package.json",
      ),
      { version: "0.1.0", pi: { extensions: ["./extension.mjs"] } },
    );
  }
  if (args[0] === "remove") {
    const data = JSON.parse(fs.readFileSync(settings));
    data.packages = data.packages.filter((p) => p !== args[1]);
    write(settings, data);
  }
} else if (agent === "omp") {
  if (args[0] === "install") {
    state.installed = true;
    save();
  }
  if (args[1] === "uninstall") {
    state.installed = false;
    save();
  }
  if (args[1] === "list")
    output({
      npm: state.installed
        ? [
            {
              name: "@traceai/oh-my-pi-plugin",
              version: "0.1.0",
              enabled: true,
              manifest: { extensions: ["./extension.mjs"] },
            },
          ]
        : [],
    });
} else if (agent === "opencode") {
  if (args[0] === "debug") output(spec.userDir);
  if (args[0] === "plugin") {
    const file = path.join(spec.userDir, "opencode.json");
    const data = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file)) : {};
    data.plugins =
      args[1] === "add"
        ? [...(data.plugins || []), args[2]]
        : (data.plugins || []).filter((x) => x !== args[2]);
    write(file, data);
  }
} else if (agent === "codex") {
  if (args[0] === "app-server") {
    require("node:readline")
      .createInterface({ input: process.stdin })
      .on("line", (line) => {
        const req = JSON.parse(line);
        if (req.method === "initialize") output({ id: req.id, result: {} });
        if (req.method === "hooks/list")
          output({
            id: req.id,
            result: {
              data: [
                {
                  hooks: spec.codexEvents.map((event) => ({
                    eventName: event,
                    pluginId: "futureagi-tracing@futureagi-local",
                    enabled: true,
                    trustStatus: "trusted",
                  })),
                },
              ],
            },
          });
      });
  } else if (args[1] === "add") {
    state.installed = true;
    save();
  } else if (args[1] === "remove") {
    state.installed = false;
    save();
  } else if (args[1] === "list")
    output({
      installed: state.installed
        ? [
            {
              pluginId: "futureagi-tracing@futureagi-local",
              version: "0.1.0",
              enabled: true,
            },
          ]
        : [],
    });
} else if (agent === "agy") {
  if (args[1] === "install") {
    state.directory = path.join(spec.userDir, "plugins/futureagi-tracing");
    fs.cpSync(args[2], state.directory, { recursive: true });
    save();
  }
  if (args[1] === "uninstall") {
    fs.rmSync(state.directory, { recursive: true, force: true });
    state = {};
    save();
  }
  if (args[1] === "list")
    output(
      state.directory
        ? [{ name: "futureagi-tracing", path: state.directory, enabled: true }]
        : [],
    );
} else if (agent === "hermes") {
  if (args[0] === "profile") output("Path: " + spec.userDir);
  if (args[1] === "install") {
    state.installed = true;
    fs.cpSync(spec.hermesSource, path.join(spec.userDir, "plugins/futureagi"), {
      recursive: true,
    });
    save();
  }
  if (args[1] === "remove") {
    state.installed = false;
    save();
  }
  if (args[1] === "list")
    output(
      state.installed
        ? [
            {
              name: "futureagi",
              version: "0.1.1",
              status: "enabled",
              source: "user",
            },
          ]
        : [],
    );
} else if (agent === "openclaw") {
  if (args[1] === "inspect")
    output({
      plugin: {
        enabled: true,
        status: "loaded",
        trustedOfficialInstall: args[2] === "diagnostics-otel",
        packageName:
          args[2] === "diagnostics-otel"
            ? "@openclaw/diagnostics-otel"
            : "@traceai/futureagi-openclaw",
        version: args[2] === "diagnostics-otel" ? "2026.10.5" : "0.1.0",
      },
    });
} else if (agent === "npm") {
  // Only the adapter's reviewed local distribution is installed. No registry access.
  const prefix = args[args.indexOf("--prefix") + 1];
  if (!prefix?.startsWith(spec.home + "/"))
    throw Error("fixture install outside home");
  fs.cpSync(
    spec.grokSource,
    path.join(prefix, "node_modules/@traceai/grok-plugin"),
    { recursive: true, filter: (src) => !src.includes("/node_modules/") },
  );
} else if (agent === "grok") {
  if (args[1] === "install") {
    state.source = args[2];
    save();
  }
  if (args[1] === "uninstall") {
    state = {};
    save();
  }
  if (args[1] === "list")
    output(
      state.source
        ? [
            {
              name: "futureagi-grok",
              source: state.source,
              status: "installed",
            },
          ]
        : [],
    );
  if (args[0] === "inspect")
    output("futureagi-grok (user, enabled)\nfile  plugin: futureagi-grok");
}
