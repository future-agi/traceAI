import * as vscode from 'vscode';
import { dirname, join } from 'node:path';
import { disable, enable, resolveConfig, readJson, updateJson, deliveryStatePath } from '@traceai/agent-core';
import { startRelay, type RelayState } from './relay';
import { installationPath, heartbeatPath, readBootstrap, readInstallation, synchronize, type Heartbeat } from './state';

let stop: (() => Promise<void>) | undefined;
export async function activate(context: vscode.ExtensionContext) {
  // Remote windows run Copilot's exporter on the remote host, which cannot reach this loopback relay.
  // Workspace trust is not a condition: the relay reads no workspace files or workspace settings.
  if (vscode.env.remoteName) return;
  const bootstrap = await readBootstrap().catch(() => undefined);
  const env = { ...process.env, ...(bootstrap ? { FI_AGENT_CONFIG_DIR: bootstrap.configDir, XDG_STATE_HOME: dirname(dirname(bootstrap.stateDir)) } : {}) };
  const path = installationPath({ env }), heartbeat = bootstrap ? join(bootstrap.stateDir, 'copilot-vscode-relay.json') : heartbeatPath({ env });
  const record = await readInstallation(path).catch(() => undefined);
  if (!record || bootstrap && bootstrap.id !== record.id) return;
  const options = { agent: 'copilot-vscode', env, projectDir: record.scope === 'project' ? record.projectDir : undefined };
  const bar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 10);
  bar.command = 'futureagi.tracing.status';
  bar.text = 'Future AGI: starting';
  bar.show();
  context.subscriptions.push(bar);
  let active = false, disposed = false, busy = false;
  let state: RelayState = { received: 0, exported: 0, dropped: 0 };
  let relay: Awaited<ReturnType<typeof startRelay>> | undefined;
  const config = async () => {
    const current = await readInstallation(path);
    if (!current || current.id !== record.id) throw new Error('INSTALLATION_CHANGED');
    return resolveConfig(options);
  };
  const tick = async () => {
    if (busy || disposed) return;
    busy = true;
    try {
      const resolved = await config();
      active = resolved.enabled;
      if (!relay) {
        try {
          relay = await startRelay({ port: record.port, token: record.token, config,
            projectDir: record.scope === 'project' ? record.projectDir : undefined,
            onState: async (next, resolved) => {
              state = next;
              if (resolved) await updateJson(deliveryStatePath(resolved), () => ({
                activatedAt: new Date().toISOString(), acceptedAt: next.lastExport,
                exported: next.exported, dropped: next.dropped, lastError: next.lastError, pending: 0
              }));
            }
          });
        } catch {
          // One application-wide listener, not one exporter per window. A remaining window
          // retries after the owner closes; no process, service or login item is installed.
          const hb = await readJson<Heartbeat>(heartbeat).catch(() => undefined);
          const other = hb?.id === record.id && Date.now() - Date.parse(hb.at) < 10000;
          bar.text = other ? 'Future AGI: shared relay' : 'Future AGI: unavailable';
          bar.tooltip = other ? 'Another local VS Code window owns the tracing relay.' : 'Relay port unavailable. Run fi-agent doctor copilot-vscode.';
          return;
        }
      }
      if (!active) relay.fence();
      const capture = resolved.captureContent && !resolved.hideInputs && !resolved.hideOutputs && !resolved.hideErrorDetails;
      const conflicts = await synchronize(path, active, capture);
      if (disposed) return;
      bar.text = active ? 'Future AGI: tracing' : 'Future AGI: disabled';
      bar.tooltip = conflicts.length ? 'Tracing settings were edited. Run fi-agent doctor copilot-vscode.' :
        (active ? (capture ? 'Content capture on. Click for tracing status.' : 'Structure-only tracing. Click for tracing status.') : 'Tracing disabled.');
      await updateJson<Heartbeat>(heartbeat, () => ({ id: record.id, at: new Date().toISOString(), active,
        exported: state.exported, dropped: state.dropped, lastExport: state.lastExport, lastError: state.lastError }));
    } catch (error) {
      const code = error instanceof Error && /^[A-Z][A-Z0-9_]{0,80}$/.test(error.message) ? error.message : 'TICK_FAILED';
      if (code === 'INSTALLATION_CHANGED') {
        active = false;
        bar.text = 'Future AGI: unavailable';
        await relay?.stop();
        relay = undefined;
      } else {
        // The relay checks enablement, destination and privacy for every export, so a transient
        // lifecycle error (e.g. enable/disable holding the settings lock) must not close the
        // listener: Copilot would get connection refused and drop spans until the next tick.
        console.warn(`Future AGI tracing check failed (${code}); retrying.`);
        bar.tooltip = 'Tracing check failed; retrying. Run fi-agent doctor copilot-vscode if this persists.';
      }
    } finally { busy = false; }
  };
  context.subscriptions.push(vscode.commands.registerCommand('futureagi.tracing.status', () => {
    void vscode.window.showInformationMessage(`Future AGI tracing ${active ? 'enabled' : 'disabled'}. Exported spans in this relay: ${state.exported}. Last export: ${state.lastExport ?? 'not observed'}. ${state.lastError ? 'Run fi-agent doctor copilot-vscode for diagnostics.' : ''}`);
  }));
  context.subscriptions.push(vscode.commands.registerCommand('futureagi.tracing.toggle', async () => {
    try {
      if ((await config()).enabled) { await disable(options); relay?.fence(); } else await enable(options);
      await tick();
      void vscode.window.showInformationMessage('Tracing preference updated. Reload VS Code to restart native Copilot telemetry.');
    } catch { void vscode.window.showWarningMessage('Tracing could not be changed. Run fi-agent doctor copilot-vscode.'); }
  }));
  const timer = setInterval(() => { void tick(); }, 2000);
  timer.unref();
  stop = async () => {
    clearInterval(timer);
    if (disposed) return;
    disposed = true;
    if (!relay) return;
    await relay.stop();
    await updateJson<Heartbeat>(heartbeat, () => ({ id: record.id, at: new Date().toISOString(), active: false,
      exported: state.exported, dropped: state.dropped, lastExport: state.lastExport })).catch(() => {});
  };
  context.subscriptions.push({ dispose: () => { void stop?.(); } });
  await tick();
}
export async function deactivate() { await stop?.(); stop = undefined; }
