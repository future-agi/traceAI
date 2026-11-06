#!/usr/bin/env node
import { createInterface } from "node:readline/promises";
import { stdin, stdout, stderr } from "node:process";
import {
  setup,
  inspectStatus,
  enable,
  disable,
  resolveConfig,
  configPaths,
  readStore,
  readSettings,
  credentialPair,
  endpoint,
  endpointFromBase,
  DEFAULT_ENDPOINT,
  type ResolveOptions,
  type SetupOptions,
  type Credentials,
} from "@traceai/agent-core";
import { adapterRelease, adapterRegistry, loadAdapter } from "./registry.js";

const usage = `Future AGI agent tracing\n\n  fi-agent setup <agent> --project <name>\n  fi-agent status [agent] [--json] [--check]\n  fi-agent doctor <agent>\n  fi-agent enable|disable|update|uninstall <agent>\n  fi-agent run <agent> -- <agent arguments>\n  fi-agent open [--last]\n\nSetup options: --scope user|project, --project-dir <path>, --base-url <origin>,\n               --no-capture-content, --configure-only\nKeys: https://app.futureagi.com/dashboard/keys\nCredentials use masked terminal entry or FI_API_KEY + FI_SECRET_KEY; never argv.\n`;
interface Parsed {
  command: string;
  agent?: string;
  values: Record<string, string>;
  flags: Set<string>;
  rest: string[];
}
function parse(args: string[]): Parsed {
  const [command = "help", ...items] = args,
    values: Record<string, string> = {},
    flags = new Set<string>();
  let agent: string | undefined;
  const rest: string[] = [];
  const takesValue = new Set([
    "--project",
    "--scope",
    "--project-dir",
    "--base-url",
  ]);
  const booleans = new Set([
    "--capture-content",
    "--no-capture-content",
    "--configure-only",
    "--json",
    "--check",
    "--last",
    "--help",
  ]);
  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    if (item === "--") {
      rest.push(...items.slice(i + 1));
      break;
    }
    if (takesValue.has(item)) {
      if (!items[i + 1] || items[i + 1].startsWith("--"))
        throw new Error("MISSING_OPTION_VALUE");
      values[item] = items[++i];
    } else if (booleans.has(item)) flags.add(item);
    else if (item.startsWith("-")) throw new Error("UNKNOWN_OPTION");
    else if (!agent) agent = item;
    else throw new Error("UNEXPECTED_ARGUMENT");
  }
  if (values["--scope"] && !["user", "project"].includes(values["--scope"]))
    throw new Error("INVALID_SCOPE");
  return { command, agent, values, flags, rest };
}
async function ask(text: string): Promise<string> {
  const rl = createInterface({ input: stdin, output: stderr });
  try {
    return (await rl.question(text)).trim();
  } finally {
    rl.close();
  }
}
/** Raw TTY capture does not echo secrets, place them in argv, history, or agent conversations. */
async function masked(text: string): Promise<string> {
  if (!stdin.isTTY || !stderr.isTTY)
    throw new Error("HEADLESS_CREDENTIALS_REQUIRED");
  stderr.write(text);
  const wasRaw = stdin.isRaw;
  stdin.setRawMode(true);
  stdin.resume();
  return new Promise((resolve, reject) => {
    let value = "";
    const done = (error?: Error): void => {
      stdin.off("data", data);
      stdin.setRawMode(!!wasRaw);
      stdin.pause();
      stderr.write("\n");
      error ? reject(error) : resolve(value);
    };
    const data = (chunk: Buffer): void => {
      for (const c of chunk.toString("utf8")) {
        if (c === "\u0003" || c === "\u0004") {
          done(new Error("CANCELLED"));
          return;
        }
        if (c === "\r" || c === "\n") {
          done();
          return;
        }
        if (c === "\u007f" || c === "\b") value = value.slice(0, -1);
        else if (c >= " " && value.length < 4096) value += c;
      }
    };
    stdin.on("data", data);
  });
}
function output(value: unknown, json: boolean): void {
  if (json) {
    stdout.write(JSON.stringify(value) + "\n");
    return;
  }
  const obj = value as Record<string, unknown>;
  for (const [key, val] of Object.entries(obj))
    if (val !== undefined)
      stdout.write(
        `${key}: ${typeof val === "object" ? JSON.stringify(val) : val}\n`,
      );
}
export async function main(args = process.argv.slice(2)): Promise<number> {
  const p = parse(args);
  if (p.command === "help" || p.command === "--help" || p.flags.has("--help")) {
    stdout.write(usage);
    return 0;
  }
  if (
    ![
      "setup",
      "status",
      "doctor",
      "enable",
      "disable",
      "update",
      "uninstall",
      "run",
      "open",
    ].includes(p.command)
  )
    throw new Error("UNKNOWN_COMMAND");
  if (p.command === "open") {
    output(
      {
        code: "PRODUCT_LINK_NOT_RESOLVED",
        detail: "No verified product readback link is available.",
      },
      p.flags.has("--json"),
    );
    return 2;
  }
  if (p.command === "status" && !p.agent) {
    output(
      {
        agents: adapterRegistry.map((a) => ({
          agent: a.id,
          qualification: a.qualified ? "QUALIFIED" : a.reason,
        })),
        detail:
          "Use status <agent> to inspect configuration, registration and exports.",
      },
      p.flags.has("--json"),
    );
    return 2;
  }
  if (!p.agent || !adapterRelease(p.agent))
    throw new Error("UNSUPPORTED_AGENT");
  const options: ResolveOptions = {
    agent: p.agent,
    // Match the directory OpenCode passes to its plugin, including for status,
    // duplicate detection and ownership-aware uninstall after project setup.
    projectDir: p.values["--project-dir"] ?? (p.agent === "opencode-v1" ? process.cwd() : undefined),
  };
  const adapter = loadAdapter(p.agent),
    json = p.flags.has("--json");
  if (p.command === "disable") {
    await disable(options);
    output({ code: "DISABLED", agent: p.agent }, json);
    return 0;
  }
  if (p.command === "status" || p.command === "doctor") {
    const status = await inspectStatus({
      ...options,
      adapter,
      check: p.command === "doctor" || p.flags.has("--check"),
    });
    output(status, json);
    return status.exitCode;
  }
  if (p.command === "setup") {
    const paths = await configPaths(options),
      store = await readStore(paths.credentials);
    let credentials: Credentials | undefined = credentialPair(process.env);
    const user = await readSettings(paths.user);
    const saved = store.profiles[user?.profile ?? store.defaultProfile ?? ""];
    const defaultProject =
      user?.projectName ?? saved?.projectName ?? "coding-agents";
    let projectName = p.values["--project"] ?? process.env.FI_PROJECT_NAME;
    const interactive = !!stdin.isTTY && !!stderr.isTTY && !json;
    if (interactive && !projectName)
      projectName =
        (await ask(
          `Future AGI project [${JSON.stringify(defaultProject)}]: `,
        )) || defaultProject;
    projectName ??= defaultProject;
    const destination = endpoint(
      p.values["--base-url"]
        ? endpointFromBase(p.values["--base-url"])
        : (process.env.FI_OTLP_ENDPOINT ??
            (process.env.FI_BASE_URL
              ? endpointFromBase(process.env.FI_BASE_URL)
              : (saved?.endpoint ?? DEFAULT_ENDPOINT))),
    );
    const captureContent = p.flags.has("--no-capture-content")
      ? false
      : p.flags.has("--capture-content")
        ? true
        : process.env.FI_AGENT_CAPTURE_CONTENT === undefined
          ? (saved?.captureContent ?? true)
          : ["true", "1"].includes(process.env.FI_AGENT_CAPTURE_CONTENT);
    if (!json)
      stderr.write(
        `Destination: ${destination}\nProject: ${JSON.stringify(projectName)}\nCapture: ${captureContent ? "content (redacted and bounded); may include file text and secrets the agent reads" : "structure only"}\n`,
      );
    if (!credentials && !saved && interactive)
      credentials = {
        apiKey: await masked("API key (hidden): "),
        secretKey: await masked("Secret key (hidden): "),
      };
    let approveDestination = false;
    if (
      saved &&
      !credentials &&
      (saved.endpoint !== destination ||
        (projectName && saved.projectName !== projectName)) &&
      interactive
    )
      approveDestination =
        (
          await ask(
            "Use this saved profile with the displayed destination and project? [y/N] ",
          )
        ).toLowerCase() === "y";
    const opts: SetupOptions = {
      ...options,
      projectName,
      baseUrl: p.values["--base-url"],
      captureContent: p.flags.has("--no-capture-content")
        ? false
        : p.flags.has("--capture-content")
          ? true
          : undefined,
      scope: p.values["--scope"] as "user" | "project" | undefined,
      configureOnly: p.flags.has("--configure-only"),
      credentials,
      approveDestination,
      adapter,
    };
    const result = await setup(opts);
    output(result, json);
    if (result.configured && !result.registered && !opts.configureOnly && !json)
      stderr.write(
        `Registration: ${adapterRelease(p.agent)!.reason}. Configuration alone is not an installed integration.\n`,
      );
    return result.registered || (opts.configureOnly && result.configured)
      ? 0
      : 2;
  }
  if (p.command === "enable") {
    await enable(options);
    output(await inspectStatus({ ...options, adapter }), json);
    return adapter ? 0 : 2;
  }
  if (!adapter) {
    output({ code: adapterRelease(p.agent)!.reason }, json);
    return 2;
  }
  const config = await resolveConfig(options),
    context = {
      config,
      scope: "user" as const,
      projectDir: options.projectDir,
    };
  if (p.command === "uninstall") {
    await disable(options);
    const result = await adapter.uninstall(context);
    output(result, json);
    return result.registered ? 2 : 0;
  }
  if (p.command === "update") {
    const result = await adapter.install(context);
    output(result, json);
    return result.registered ? 0 : 2;
  }
  if (p.command === "run")
    return adapter.run ? adapter.run(context, p.rest) : 2;
  return 2;
}
if (require.main === module)
  main()
    .then((code) => {
      process.exitCode = code;
    })
    .catch((error) => {
      // Never print user-supplied argv or arbitrary filesystem/provider error messages.
      const code =
        error instanceof Error && /^[A-Z][A-Z0-9_]{0,80}$/.test(error.message)
          ? error.message
          : "OPERATION_FAILED";
      if (process.argv.includes("--json"))
        stdout.write(JSON.stringify({ code }) + "\n");
      else stderr.write(`${code}\n`);
      process.exitCode = 2;
    });
