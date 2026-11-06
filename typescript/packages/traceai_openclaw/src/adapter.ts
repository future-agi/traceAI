import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { randomUUID } from 'node:crypto';
import { basename, dirname, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { readFile, unlink } from 'node:fs/promises';
import * as json5 from 'jju';
import { withBootstrap } from './node-options';
import { createLauncherDelivery } from './launcher-delivery';
import {
  readProtected, writeProtected, contentHash, readJson, updateJson, collectionEnabled, sessionId,
  type CoreAdapter, type AdapterContext, type AdapterInspection,
} from '@traceai/agent-core';

export const HOST_VERSION = '2026.10.5';
/** The 2026 native diagnostics contract; individual payloads remain shape-gated. */
export function supportedHostVersion(version: unknown): version is string {
  if (typeof version !== 'string') return false;
  const match = /^2026\.(10|11|12)\.(\d+)$/.exec(version);
  return !!match && (Number(match[1]) > 10 || Number(match[2]) >= 5);
}
const VERSION = '0.1.0'; // x-release-please-version
const EXPORTER_CONFLICT = 'EXISTING_OPENCLAW_EXPORTER: diagnostics.otel.enabled is true. Keep your existing direct exporter instead of this launcher, or deliberately disable that route in OpenClaw before running setup again.';
const PLUGIN = 'futureagi-openclaw';
const exec = promisify(execFile);
type HostConfig = Record<string, any>;
interface Ownership { path: string; before?: unknown; installed: unknown }
export type Command = (args: string[]) => Promise<string>;
export function parseHost(text: string | undefined): HostConfig {
  const value = json5.parse(text ?? '{}');
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('INVALID_OPENCLAW_CONFIG');
  return value;
}
export function nativePath(context: AdapterContext, env: NodeJS.ProcessEnv = process.env): string {
  // Core resolves CONFIG_PATH > STATE_DIR > OPENCLAW_HOME > OS home, including
  // tilde expansion. Use that same directory for preferences and native settings.
  return join(dirname(context.config.paths.user), basename(env.OPENCLAW_CONFIG_PATH || 'openclaw.json'));
}
function ownershipPath(context: AdapterContext): string { return join(context.config.paths.configDir, 'openclaw-registration.json'); }
function same(a: unknown, b: unknown): boolean { return JSON.stringify(a) === JSON.stringify(b); }
function hash(text?: string): string | null { return text === undefined ? null : contentHash(text); }
function hasOtherExporter(config: HostConfig): boolean {
  return config.diagnostics?.otel?.enabled === true;
}
export function overlay(config: HostConfig, captureContent: boolean, enabled: boolean): HostConfig {
  return {
    ...config,
    diagnostics: {
      ...config.diagnostics,
      enabled,
      otel: {
        enabled, traces: true, metrics: false, logs: false, captureContent,
        protocol: 'http/protobuf', serviceName: 'openclaw',
      },
    },
  };
}
export function localArguments(args: string[]): { args: string[]; session: string } {
  if (args[0] !== 'agent' || args[1] === 'exec' || !args.includes('--local')) throw new Error('OPENCLAW_LOCAL_AGENT_ONLY');
  for (const flag of ['--profile', '--dev', '--session-key', '--to', '-t', '--deliver'])
    if (args.some((a) => a === flag || a.startsWith(flag + '='))) throw new Error('OPENCLAW_UNSUPPORTED_ROUTING');
  const at = args.indexOf('--session-id');
  if (args.some((a) => a.startsWith('--session-id=')) || (at >= 0 && args.lastIndexOf('--session-id') !== at))
    throw new Error('OPENCLAW_SESSION_ARGUMENT_REQUIRED');
  const session = at < 0 ? randomUUID() : args[at + 1];
  if (!session || session.startsWith('-') || session.length > 256) throw new Error('OPENCLAW_SESSION_ARGUMENT_REQUIRED');
  return { args: at < 0 ? [...args, '--session-id', session] : [...args], session };
}

/** Setup uses the host installer. Runtime is a bounded, in-process preloaded SDK,
 * not a daemon, collector, hook script, provider proxy, or second instrumentor. */
export function createOpenClawAdapter(command?: Command): CoreAdapter {
  const call: Command = command ?? (async (args) => {
    const env = { ...process.env };
    delete env.FI_API_KEY; delete env.FI_SECRET_KEY;
    try {
      const result = await exec('openclaw', args, { env, timeout: 180000, maxBuffer: 1024 * 1024 });
      return result.stdout;
    } catch { throw new Error('OPENCLAW_COMMAND_FAILED'); }
  });
  async function plugin(id: string): Promise<any | undefined> {
    try { return JSON.parse(await call(['plugins', 'inspect', id, '--json'])).plugin; } catch { return undefined; }
  }
  async function inspect(context: AdapterContext): Promise<AdapterInspection> {
    const [official, lifecycle, record, hostOutput] = await Promise.all([
      plugin('diagnostics-otel'), plugin(PLUGIN), readJson<Ownership>(ownershipPath(context)),
      call(['--version']).catch(() => ''),
    ]);
    const hostVersion = /\b(\d{4}\.\d+\.\d+)\b/.exec(hostOutput)?.[1];
    const current = parseHost(await readProtected(nativePath(context), false));
    const registered = supportedHostVersion(hostVersion) && !!official?.enabled && official?.status === 'loaded' && supportedHostVersion(official?.version) &&
      official?.trustedOfficialInstall === true && !!lifecycle?.enabled && lifecycle?.status === 'loaded' &&
      lifecycle?.packageName === '@traceai/futureagi-openclaw' && lifecycle?.version === VERSION && !!record && record.path === nativePath(context) && same(current.diagnostics, record.installed);
    return {
      registered, activated: false, hostVersion,
      trust: official?.enabled && lifecycle?.enabled ? 'confirmed' : 'unknown',
      duplicate: hasOtherExporter(current),
      detail: hasOtherExporter(current) ? 'EXISTING_OPENCLAW_EXPORTER' : registered ? undefined : 'OPENCLAW_SETUP_REQUIRED',
      recovery: registered
        ? 'Run with fi-agent run openclaw -- agent --local ... . One prompt trace per invocation, including native retries. No gateway/session-hook permission was granted.'
        : undefined,
    };
  }
  return {
    id: 'openclaw', version: VERSION, requiresWorker: false,
    diagnostics: {
      OPENCLAW_SETUP_REQUIRED: 'Native plugin registration missing, modified, or incompatible; run setup again. The native direct exporter is disabled.',
      OPENCLAW_LIFECYCLE_REVIEW_REQUIRED: 'Review the staged lifecycle package and complete the native plugin consent steps in recovery, then rerun setup/update. No trust was granted automatically.',
    },
    setupErrors: {
      EXISTING_OPENCLAW_EXPORTER: EXPORTER_CONFLICT,
      OPENCLAW_REGISTRATION_MODIFIED: 'OpenClaw diagnostics settings changed since setup. Review your current route and restore the owned registration before retrying; no settings were overwritten.',
      OPENCLAW_PLUGIN_REQUIRES_ATTENTION: 'Review diagnostics-otel in OpenClaw. This integration requires an enabled, trusted official plugin in the 2026 version line; it will not replace an untrusted or incompatible plugin.',
      OPENCLAW_LIFECYCLE_REQUIRES_ATTENTION: 'The futureagi-openclaw plugin ID belongs to another package. Review it in OpenClaw; setup will not replace it.',
    },
    async detect() {
      const version = /\b(\d{4}\.\d+\.\d+)\b/.exec(await call(['--version']))?.[1];
      return { supported: supportedHostVersion(version), version, reason: supportedHostVersion(version) ? undefined : 'OPENCLAW_VERSION_NOT_QUALIFIED' };
    },
    async install(context) {
      const path = nativePath(context);
      const original = parseHost(await readProtected(path, false));
      if (hasOtherExporter(original)) throw new Error('EXISTING_OPENCLAW_EXPORTER');
      const previous = await readJson<Ownership>(ownershipPath(context));
      if (previous && (previous.path !== path || !same(original.diagnostics, previous.installed)))
        throw new Error('OPENCLAW_REGISTRATION_MODIFIED');
      const official = await plugin('diagnostics-otel');
      if (!official) {
        const hostVersion = /\b(\d{4}\.\d+\.\d+)\b/.exec(await call(['--version']))?.[1];
        if (!supportedHostVersion(hostVersion)) throw new Error('OPENCLAW_PLUGIN_REQUIRES_ATTENTION');
        await call(['plugins', 'install', `clawhub:@openclaw/diagnostics-otel@${hostVersion}`]);
      } else if (!supportedHostVersion(official.version) || !official.enabled || official.trustedOfficialInstall !== true)
        throw new Error('OPENCLAW_PLUGIN_REQUIRES_ATTENTION');
      const lifecycle = await plugin(PLUGIN);
      if (lifecycle && lifecycle.packageName !== '@traceai/futureagi-openclaw')
        throw new Error('OPENCLAW_LIFECYCLE_REQUIRES_ATTENTION');
      if (!lifecycle || lifecycle.version !== VERSION) {
        const distribution = join(context.config.paths.configDir, 'openclaw-lifecycle', VERSION);
        for (const name of ['package.json', 'openclaw.plugin.json', 'index.cjs']) {
          const asset = await readFile(join(__dirname, '..', 'native', name), 'utf8');
          await writeProtected(join(distribution, name), asset);
        }
        // The host requires explicit consent for non-ClawHub code. Forward its
        // own TTY prompt; never add --force or accept hook capabilities silently.
        // Local-path install cannot overwrite without --force (which also bypasses
        // source consent). Use the host's confirmed uninstall/install flow instead.
        const remove = lifecycle ? `run openclaw plugins uninstall ${PLUGIN} interactively, then ` : '';
        const quoted = "'" + distribution.replace(/'/g, "'\\''") + "'";
        const detail = `Review ${distribution}, then ${remove}run openclaw plugins install ${quoted} interactively, run openclaw plugins enable ${PLUGIN}, and rerun setup/update.`;
        if (!process.stdin.isTTY || !process.stdout.isTTY || process.argv.includes('--json'))
          return { registered: false, trust: 'required', detail: 'OPENCLAW_LIFECYCLE_REVIEW_REQUIRED', recovery: detail };
        const installEnv = { ...process.env };
        delete installEnv.FI_API_KEY; delete installEnv.FI_SECRET_KEY;
        const confirmed = (args: string[]) => new Promise<boolean>((done) => {
          const child = spawn('openclaw', args, { env: installEnv, stdio: 'inherit' });
          child.once('error', () => done(false));
          child.once('close', (code) => done(code === 0));
        });
        if (lifecycle && !(await confirmed(['plugins', 'uninstall', PLUGIN])))
          return { registered: false, trust: 'required', detail: 'OPENCLAW_LIFECYCLE_REVIEW_REQUIRED', recovery: detail };
        if (!(await confirmed(['plugins', 'install', distribution])))
          return { registered: false, trust: 'required', detail: 'OPENCLAW_LIFECYCLE_REVIEW_REQUIRED', recovery: detail };
        // OpenClaw can preserve a disabled entry across uninstall/reinstall.
        // Enable only our reviewed plugin through the native consent-aware command.
        if (!(await confirmed(['plugins', 'enable', PLUGIN])))
          return { registered: false, trust: 'required', detail: 'OPENCLAW_LIFECYCLE_REVIEW_REQUIRED', recovery: detail };
      }
      // Installer owns plugins.allow; never replace or create an exclusive allowlist.
      // Re-read after it writes and preserve JSON5 comments and all unrelated settings.
      const text = await readProtected(path, false);
      const current = parseHost(text);
      if (!same(current.diagnostics, original.diagnostics)) throw new Error('OPENCLAW_REGISTRATION_MODIFIED');
      if (hasOtherExporter(current)) throw new Error('EXISTING_OPENCLAW_EXPORTER');
      const installed = { ...current.diagnostics, otel: { ...current.diagnostics?.otel, enabled: false } };
      const record: Ownership = previous ?? { path, before: current.diagnostics, installed };
      await writeProtected(path, json5.update(text ?? '{}', { ...current, diagnostics: installed }), hash(text), false);
      await updateJson<Ownership>(ownershipPath(context), () => ({ ...record, installed }));
      return inspect(context);
    },
    inspect,
    async uninstall(context) {
      const record = await readJson<Ownership>(ownershipPath(context));
      if (!record) return { registered: false, detail: 'No owned registration. Official plugins left untouched.' };
      const text = await readProtected(record.path, false), current = parseHost(text);
      if (!same(current.diagnostics, record.installed)) throw new Error('OPENCLAW_REGISTRATION_MODIFIED');
      if (record.before === undefined) delete current.diagnostics;
      else current.diagnostics = record.before;
      await writeProtected(record.path, json5.update(text ?? '{}', current), hash(text), false);
      await unlink(ownershipPath(context));
      return { registered: false, detail: 'Owned diagnostics settings removed. Shared native plugins remain installed but cannot export without an explicit launcher.' };
    },
    async run(context, input) {
      const { args, session } = localArguments(input);
      if (process.env.OPENCLAW_OTEL_PRELOADED === '1') throw new Error('OPENCLAW_EXISTING_PRELOAD');
      const nodeOptions = withBootstrap(process.env.NODE_OPTIONS, pathToFileURL(join(__dirname, '..', 'native', 'bootstrap.mjs')).href);
      if (!(await inspect(context)).registered) throw new Error('OPENCLAW_SETUP_REQUIRED');
      const source = parseHost(await readProtected(nativePath(context), false));
      const enabled = collectionEnabled(context.config) && process.env.OTEL_SDK_DISABLED !== 'true';
      const temp = join(dirname(nativePath(context)), `.futureagi-run-${randomUUID()}.json`);
      await writeProtected(temp, JSON.stringify(overlay(source, context.config.captureContent, enabled)), null);
      // The launcher already exists for signal/exit propagation. It also owns
      // the bounded exporter, so an explicit host exit cannot discard its queue.
      let delivery: Awaited<ReturnType<typeof createLauncherDelivery>> | undefined;
      let exitCode = 127;
      const env: NodeJS.ProcessEnv = {
        ...process.env,
        OPENCLAW_CONFIG_PATH: temp,
        // Disable native SDK creation even if our bootstrap fails. No fallback exporter.
        OPENCLAW_OTEL_PRELOADED: '1', OTEL_SDK_DISABLED: 'true',
        // Keep the SDK in the process that actually runs the agent, not a CLI
        // bootstrap parent. Both switches are supported by the pinned host.
        OPENCLAW_NO_RESPAWN: '1', NODE_DISABLE_COMPILE_CACHE: '1',
        OTEL_METRICS_EXPORTER: 'none', OTEL_LOGS_EXPORTER: 'none',
        FI_OPENCLAW_BOOTSTRAP: '1', FI_OPENCLAW_SESSION: session,
        FI_OPENCLAW_PROJECT_DIR: context.projectDir ?? '',
        FI_OPENCLAW_NODE_OPTIONS: process.env.NODE_OPTIONS ?? '',
        NODE_OPTIONS: nodeOptions,
      };
      try {
        if (enabled) {
          try {
            const opaqueSession = context.config.identityKey
              ? sessionId(Buffer.from(context.config.identityKey, 'hex'), 'openclaw', session) : undefined;
            delivery = await createLauncherDelivery(context.config, opaqueSession);
            env.FI_OPENCLAW_BRIDGE_FD = '3';
            env.FI_OPENCLAW_BRIDGE_KEY = delivery.key;
          } catch { /* Capture setup must not prevent a local model/tool run. */ }
        }
        exitCode = await new Promise<number>((resolveCode) => {
          const child = spawn('openclaw', args, { env, stdio: ['inherit', 'inherit', 'inherit', delivery?.fd ?? 'ignore'] });
          delete env.FI_OPENCLAW_BRIDGE_KEY;
          const interrupt = () => child.kill('SIGINT'), terminate = () => child.kill('SIGTERM');
          const cleanup = () => { process.off('SIGINT', interrupt); process.off('SIGTERM', terminate); };
          process.on('SIGINT', interrupt); process.on('SIGTERM', terminate);
          child.once('error', () => { cleanup(); resolveCode(127); });
          child.once('close', (code, signal) => {
            cleanup();
            resolveCode(code ?? (signal === 'SIGINT' ? 130 : 143));
          });
        });
        return exitCode;
      } finally {
        await delivery?.finish(exitCode).catch(() => {});
        await unlink(temp).catch(() => {});
      }
    },
  };
}
export const openclawAdapter = createOpenClawAdapter();
