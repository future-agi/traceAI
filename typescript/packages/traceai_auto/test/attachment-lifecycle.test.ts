import { spawnSync } from "node:child_process";
import { resolve } from "node:path";

const root = resolve(__dirname, "..");
function run(mode: string, preload?: string) {
  const code = `
    const assert = require('node:assert/strict');
    const { BasicTracerProvider, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
    const { AttachmentManager } = require('@traceai/fi-core/attachments');
    const auto = require('./dist/src/index.js');
    (async () => {
      const manager = new AttachmentManager({ enabled: false, environment: () => ({}) });
      const host = new BasicTracerProvider();
      if (${JSON.stringify(mode)} === 'host') host.attachments = manager;
      const options = { exporter: new InMemorySpanExporter(), instrumentations: ['wire'], configFile: '/nonexistent' };
      if (${JSON.stringify(mode)} === 'borrowed') options.attachmentManager = manager;
      if (${JSON.stringify(mode)} === 'host') options.provider = host;
      let network = 0;
      globalThis.fetch = () => { network++; throw new Error('unexpected network'); };
      const provider = auto.init(options);
      assert.ok(provider);
      const selected = auto.getAttachmentManager();
      assert.ok(selected);
      if (['host', 'borrowed'].includes(${JSON.stringify(mode)})) assert.equal(selected, manager);
      else assert.equal(selected, provider.attachments);
      assert.equal(await selected.ready(100), false);
      assert.equal(selected.status().state, 'disabled');
      assert.equal(auto.report().attachments.state, 'disabled'); assert.equal(auto.report().attachments.transport, 'shared_manager');
      await auto.forceFlush();
      await auto.shutdown(); await auto.shutdown();
      assert.equal(selected.status().state, 'disabled');
      assert.equal(network, 0);
      await manager.shutdown(); await host.shutdown();
      process.stdout.write('LIFECYCLE_OK');
    })().catch(() => { process.stderr.write('LIFECYCLE_FAILED'); process.exitCode = 1; });
  `;
  return spawnSync(process.execPath, [...(preload ? ['--require', preload] : []), '-e', code], {
    cwd: root, encoding: 'utf8', timeout: 15000,
    env: { ...process.env, FI_ATTACHMENTS_ENABLED: 'false', FI_API_KEY: '', FI_SECRET_KEY: '', FI_CONFIG_FILE: '/nonexistent', FI_AUTO_DEBUG: 'false' },
  });
}
test.each(['owned', 'host', 'borrowed'])('%s manager uses one selected runtime and preserves lifecycle ownership', mode => {
  const result = run(mode); expect(result.error).toBeUndefined(); expect(result.status).toBe(0); expect(result.stdout).toContain('LIFECYCLE_OK');
});
