#!/usr/bin/env node
import { init, forceFlush, report, shutdown } from "./index";
import { trace } from "@opentelemetry/api";
import { inventory } from "./libraries";
import { resolveConfig } from "./config";
import { spawn } from "node:child_process";

async function main(): Promise<void> {
  const command = process.argv[2] ?? "doctor";
  if (command === "bootstrap") {
    const rows = inventory(resolveConfig());
    const missing = rows.filter(r => r.status === "missing_instrumentor").map(r => r.package!);
    console.log(JSON.stringify({ integrations: rows, install: missing.map(pkg => `npm install ${pkg}`) }, null, 2));
    if (process.argv.includes("--install") && missing.length) {
      const manager = process.argv.find(arg => arg.startsWith("--package-manager="))?.split("=")[1] ?? "npm";
      if (!["npm", "pnpm", "yarn"].includes(manager)) throw new Error("Unsupported package manager");
      const code = await new Promise<number | null>((resolve, reject) => {
        const child = spawn(manager, [manager === "npm" ? "install" : "add", ...missing], { cwd: process.cwd(), stdio: "inherit", shell: false });
        child.on("error", reject); child.on("close", resolve);
      });
      if (code !== 0) process.exitCode = 1;
    }
    return;
  }
  if (command !== "doctor") {
    console.error("Usage: traceai-auto doctor [--no-check] | bootstrap"); process.exitCode = 1; return;
  }
  init();
  const initial = report();
  if (initial.active && !process.argv.includes("--no-check")) {
    const span = trace.getTracer("@traceai/auto/doctor").startSpan("traceai.auto.doctor"); span.end();
    await forceFlush();
  }
  console.log(JSON.stringify(report(), null, 2));
  await shutdown();
}
main().catch(() => { console.error("[traceai:auto] Diagnostic check failed; verify the resolved configuration."); process.exitCode = 1; });
