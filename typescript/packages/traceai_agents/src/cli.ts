#!/usr/bin/env node
import { createInterface } from "node:readline/promises";
import { resolve } from "node:path";
import { stdin, stdout, stderr } from "node:process";
import {
  setup,
  inspectStatus,
  enable,
  disable,
  uninstall,
  resolveConfig,
  readSetupDefaults,
  environmentCredentialsOnly,
  credentialPair,
  endpoint,
  endpointFromBase,
  DEFAULT_ENDPOINT,
  type ResolveOptions,
  type SetupOptions,
  type Credentials,
} from "@traceai/agent-core";
import { discoverHermesDirectory } from "@traceai/hermes-agent";
import { adapterRelease, adapterRegistry, loadAdapter } from "./registry.js";

const usage = `Future AGI agent tracing\n\n  fi-agent setup <agent> --project <name>\n  fi-agent status [agent] [--json] [--check]\n  fi-agent doctor <agent>\n  fi-agent enable|disable|update|uninstall <agent>\n  fi-agent run <agent> -- <agent arguments>\n  fi-agent open [--last]\n\nSetup options: --scope user|project, --project-dir <path>, --agent-dir <path>, --base-url <origin>,\n               --no-capture-content, --configure-only, --replace-credentials, --user-only\nKeys: https://app.futureagi.com/dashboard/keys\nCredentials use masked terminal entry or FI_API_KEY + FI_SECRET_KEY; never argv.\n`;
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
    "--agent-dir",
    "--base-url",
  ]);
  const booleans = new Set([
    "--capture-content",
    "--no-capture-content",
    "--configure-only",
    "--replace-credentials",
    "--user-only",
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
  const wasRaw = stdin.isRaw;
  return new Promise((resolve, reject) => {
    let value = "", finished = false;
    const restore = (): void => { stdin.setRawMode(!!wasRaw); };
    const done = (error?: Error): void => {
      if (finished) return;
      finished = true;
      stdin.off("data", data);
      stdin.off("end", ended);
      stdin.off("error", failed);
      process.off("exit", restore);
      try { restore(); } catch { error ??= new Error("TERMINAL_RESTORE_FAILED"); }
      stdin.pause();
      try { stderr.write("\n"); } catch { /* Restore even when the terminal closed. */ }
      error ? reject(error) : resolve(value);
    };
    const ended = (): void => done(new Error("CANCELLED"));
    const failed = (): void => done(new Error("TERMINAL_READ_FAILED"));
    const data = (chunk: Buffer): void => {
      for (const c of chunk.toString("utf8")) {
        if (c === "\u0003" || c === "\u0004") { ended(); return; }
        if (c === "\r" || c === "\n") { done(); return; }
        if (c === "\u007f" || c === "\b") value = value.slice(0, -1);
        else if (c >= " " && value.length < 4096) value += c;
      }
    };
    try {
      // A visible prompt is permission to type: disable echo and attach every
      // handler first, even if stderr.write deschedules this process afterward.
      stdin.setRawMode(true);
      stdin.on("data", data);
      stdin.once("end", ended);
      stdin.once("error", failed);
      process.once("exit", restore);
      stdin.resume();
      stderr.write(text);
    } catch { failed(); }
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
    // Match host workspace discovery and inspect the active project by default.
    // --user-only ignores the repository layer entirely.
    projectDir:
      (p.values["--project-dir"] ? resolve(p.values["--project-dir"]) : undefined) ??
      (p.flags.has("--user-only") ? undefined : process.cwd()),
    agentDir: p.values["--agent-dir"],
  };
  const adapter = loadAdapter(p.agent),
    json = p.flags.has("--json");
  const release = adapterRelease(p.agent)!;
  const setupBlocker = release.setupBlocker ?? release.setupHold;
  if (setupBlocker && ["setup", "enable", "update", "run"].includes(p.command)) {
    output({ code: setupBlocker, agent: p.agent, configured: false, registered: false,
      detail: "This surface requires an independently verified GUI session. No credentials, host registration or network changes were made." }, json);
    return 2;
  }
  if (p.command === "disable") {
    await disable(options);
    output({ code: "DISABLED", agent: p.agent }, json);
    return 0;
  }
  if (p.agent === "hermes" && !options.agentDir)
    options.agentDir = await discoverHermesDirectory();
  // CLI qualification does not authorize the separate IDE/App surface. Block it
  // before config reads, credential prompts, validation and writes; disable stays offline.
  if (p.agent === "antigravity-ide") {
    output({
      code: adapterRelease(p.agent)!.reason,
      agent: p.agent,
      qualification: "BLOCKED",
      registration: "NOT_REGISTERED",
      activation: "NOT_OBSERVED",
      configuration: "NOT_INSPECTED",
      credentialCheck: "NOT_ATTEMPTED",
      detail: "Antigravity IDE and App tracing requires separate real GUI qualification. No GUI hooks or credentials are installed by this command.",
    }, json);
    return 2;
  }
  if (p.command === "uninstall") {
    // Fence active launchers first, even when profiles or project settings are broken.
    await disable(options);
    if (!adapter) {
      output({ code: adapterRelease(p.agent)!.reason }, json);
      return 2;
    }
    let result;
    if (adapter.uninstallUnconfigured) {
      try {
        await resolveConfig(options);
      } catch {
        // A missing project or broken profile cannot prevent fencing and
        // removal of the adapter's separately owned launch registration.
        result = await adapter.uninstallUnconfigured(options);
      }
    }
    result ??= await uninstall({ ...options, adapter });
    output(result, json);
    return result.registered ? 2 : 0;
  }
  if (p.command === "status" || p.command === "doctor") {
    const status = await inspectStatus({
      ...options,
      adapter,
      check: p.command === "doctor" || p.flags.has("--check"),
    });
    output({ ...status,
      ...(p.agent === "augment-ide" && status.activation === "OBSERVED" ? { activation: "HOOK_OBSERVED_EDITOR_UNVERIFIED" } : {}),
      qualification: release.qualified ? "QUALIFIED" : release.reason,
    }, json);
    return status.exitCode;
  }
  if (p.command === "setup") {
    const scope = (p.values["--scope"] ??
      (environmentCredentialsOnly(p.agent) ? "project" : undefined)) as
      | "user"
      | "project"
      | undefined;
    const {
      existing,
      old: saved,
      repositoryProfileTrusted,
    } = await readSetupDefaults({ ...options, scope });
    let credentials: Credentials | undefined = credentialPair(process.env);
    const defaultProject =
      existing?.projectName ?? saved?.projectName ?? "coding-agents";
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
              : (existing?.transport?.endpoint ??
                (existing?.transport?.baseUrl
                  ? endpointFromBase(existing.transport.baseUrl)
                  : saved?.endpoint) ??
                DEFAULT_ENDPOINT))),
    );
    const captureContent = p.flags.has("--no-capture-content")
      ? false
      : p.flags.has("--capture-content")
        ? true
        : process.env.FI_AGENT_CAPTURE_CONTENT === undefined
          ? (existing?.captureContent ?? saved?.captureContent ?? true)
          : ["true", "1"].includes(process.env.FI_AGENT_CAPTURE_CONTENT);
    if (!json)
      stderr.write(
        `Destination: ${destination}\nProject: ${JSON.stringify(projectName)}\nCapture: ${captureContent ? "content (redacted and bounded); may include file text and secrets the agent reads" : "structure only"}\n`,
      );
    if (p.flags.has("--replace-credentials") && !credentials && !interactive) {
      output(
        {
          code: "HEADLESS_CREDENTIALS_REQUIRED",
          detail:
            "Supply a new FI_API_KEY + FI_SECRET_KEY environment pair, or run setup --replace-credentials in a terminal. Saved credentials are unchanged.",
        },
        json,
      );
      return 2;
    }
    if (
      !credentials &&
      (!saved || p.flags.has("--replace-credentials")) &&
      interactive &&
      !environmentCredentialsOnly(p.agent)
    )
      credentials = {
        apiKey: await masked("API key (hidden): "),
        secretKey: await masked("Secret key (hidden): "),
      };
    let approveDestination = false;
    if (
      saved &&
      (!repositoryProfileTrusted ||
        saved.endpoint !== destination ||
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
      scope,
      configureOnly: p.flags.has("--configure-only"),
      credentials,
      approveDestination,
      adapter,
    };
    let result = await setup(opts);
    if (
      result.code === "AUTH_REJECTED" &&
      saved &&
      !credentials &&
      interactive &&
      !environmentCredentialsOnly(p.agent)
    ) {
      stderr.write(
        "Saved credentials were rejected. Enter a replacement pair; the saved profile is unchanged unless validation succeeds.\n",
      );
      result = await setup({
        ...opts,
        credentials: {
          apiKey: await masked("API key (hidden): "),
          secretKey: await masked("Secret key (hidden): "),
        },
      });
    }
    output(
      result.code === "AUTH_REJECTED"
        ? {
            ...result,
            detail:
              "Credentials rejected; saved profile is unchanged. Run setup with --replace-credentials for hidden terminal entry, or supply a new FI_API_KEY + FI_SECRET_KEY environment pair. Keep --scope project when replacing project credentials.",
          }
        : result,
      json,
    );
    if (result.registered && p.agent === "copilot" && !json)
      stderr.write(
        "Start traced sessions with: npx @traceai/agents run copilot -- <Copilot arguments>\n",
      );
    if (result.configured && !result.registered && !opts.configureOnly && !json)
      stderr.write(
        p.agent === "antigravity"
          ? `Registration: ${result.code}. ${result.diagnostics?.map(d => d.action).join(" ") || "Configuration alone is not an installed integration."}\n`
          : `Registration: ${adapterRelease(p.agent)!.reason}. Configuration alone is not an installed integration.\n`,
      );
    return (result.registered && result.trust !== "required" && result.code !== "TRUST_REQUIRED") ||
      (opts.configureOnly && result.configured)
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
  if (p.command === "run") {
    let config;
    try {
      config = await resolveConfig(options);
    } catch {
      return adapter.runUnconfigured
        ? adapter.runUnconfigured(p.rest, options.projectDir)
        : 2;
    }
    return adapter.run
      ? adapter.run(
          {
            config,
            scope: config.provenance.profile === "project" ? "project" : "user",
            projectDir: options.projectDir,
          },
          p.rest,
        )
      : 2;
  }
  const config = await resolveConfig(options),
    context = {
      config,
      scope: (p.values["--scope"] ??
        (config.provenance.profile === "project" ? "project" : "user")) as
        | "user"
        | "project",
      projectDir: options.projectDir,
    };
  if (p.command === "update") {
    const result = await (adapter.update ? adapter.update(context) : adapter.install(context));
    output(result, json);
    return result.registered && result.trust !== "required" ? 0 : 2;
  }
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
