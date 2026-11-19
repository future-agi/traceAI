#!/usr/bin/env node
import { resolveConfig, deliveryStatePath } from "@traceai/agent-core/config";
import { updateJson } from "@traceai/agent-core/files";
import type { DeliveryState } from "@traceai/agent-core";
import { normalizeHook, record } from "./events.js";
import { capture } from "./capture.js";

export async function main(): Promise<void> {
  const deadline = setTimeout(() => process.exit(0), 8000);
  deadline.unref();
  let size = 0;
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) {
    const b = Buffer.from(chunk);
    size += b.length;
    if (size > 256 * 1024) return;
    chunks.push(b);
  }
  const value = JSON.parse(Buffer.concat(chunks).toString("utf8")),
    cwd = record(value).cwd;
  const config = await resolveConfig({
    agent: "grok",
    projectDir:
      typeof cwd === "string" && cwd.startsWith("/") ? cwd : undefined,
  });
  const event = normalizeHook(value, config);
  if (event)
    await capture(event, config).catch(async (error) => {
      const code =
        error instanceof Error && /^[A-Z][A-Z0-9_]{0,80}$/.test(error.message)
          ? error.message
          : "LOCAL_CAPTURE_FAILED";
      await updateJson<DeliveryState>(
        deliveryStatePath(config),
        (old) => ({
          binding: config.deliveryBinding,
          pending: 0,
          exported: old?.exported ?? 0,
          dropped: (old?.dropped ?? 0) + 1,
          activatedAt: old?.activatedAt,
          lastAcceptedAt: old?.lastAcceptedAt,
          lastError: code,
        }),
      ).catch(() => {});
    });
}
if (require.main === module)
  main()
    .catch(() => {})
    .finally(() => {
      process.exitCode = 0;
    });
