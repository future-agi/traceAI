// Lifecycle only. All spans are produced by the official diagnostics-otel plugin.
// The symbol exists only inside an explicit `fi-agent run openclaw` process.
module.exports = {
  id: 'futureagi-openclaw',
  register(api) {
    api.registerService({
      id: 'futureagi-openclaw-flush',
      start() {},
      async stop() {
        const bridge = globalThis[Symbol.for('futureagi.openclaw.lifecycle.v1')];
        if (bridge) await bridge.flush().catch(() => {});
      },
    });
  },
};
