import { trace } from '@opentelemetry/api';
import { BasicTracerProvider } from '@opentelemetry/sdk-trace-base';
import { resourceFromAttributes } from '@opentelemetry/resources';
import { resolveConfig, sessionId, collectionEnabled } from '@traceai/agent-core';
import { LauncherCaptureProcessor } from './launcher-delivery';
import { restoreNodeOptions } from './node-options';

/** OpenClaw's existing preloaded-SDK handoff is the only global registration.
 * It is confined to an explicit child process, before the host starts, and refuses
 * an existing SDK (setGlobalTracerProvider returns false). No global is replaced.
 */
export async function bootstrap(): Promise<void> {
  if (process.env.FI_OPENCLAW_BOOTSTRAP !== '1') return;
  const projectDir = process.env.FI_OPENCLAW_PROJECT_DIR;
  const sourceSession = process.env.FI_OPENCLAW_SESSION;
  const bridgeKey = process.env.FI_OPENCLAW_BRIDGE_KEY;
  const bridgeFd = process.env.FI_OPENCLAW_BRIDGE_FD;
  delete process.env.FI_OPENCLAW_BRIDGE_KEY;
  delete process.env.FI_OPENCLAW_BRIDGE_FD;
  // Remove only our bootstrap before tools start; retain the user's memory/CA
  // options verbatim. Other preloads are rejected by the launcher.
  restoreNodeOptions(process.env);
  delete process.env.FI_OPENCLAW_BOOTSTRAP;
  delete process.env.FI_OPENCLAW_PROJECT_DIR;
  delete process.env.FI_OPENCLAW_SESSION;
  let config;
  try {
    config = await resolveConfig({ agent: 'openclaw', projectDir });
  } finally {
    delete process.env.FI_API_KEY;
    delete process.env.FI_SECRET_KEY;
  }
  if (!collectionEnabled(config) || bridgeFd !== '3' || !bridgeKey || !/^[0-9a-f]{64}$/.test(bridgeKey)) return;
  const session = sourceSession && config.identityKey
    ? sessionId(Buffer.from(config.identityKey, 'hex'), 'openclaw', sourceSession)
    : undefined;
  const processor = new LauncherCaptureProcessor(config, 3, Buffer.from(bridgeKey, 'hex'), session);
  // Empty resource here; the normalizer supplies a project-only resource. No detectors.
  const provider = new BasicTracerProvider({ resource: resourceFromAttributes({}), spanProcessors: [processor] });
  if (!trace.setGlobalTracerProvider(provider)) {
    await provider.shutdown();
    return;
  }
  process.env.OPENCLAW_OTEL_PRELOADED = '1';
  process.env.OTEL_SDK_DISABLED = 'false';
  // Capture is synchronous. The parent drains only after the host has closed,
  // including explicit process.exit; no host cleanup hook is needed for delivery.
  const flush = (): Promise<void> => processor.forceFlush();
  (globalThis as Record<symbol, unknown>)[Symbol.for('futureagi.openclaw.lifecycle.v1')] = { flush };
}
