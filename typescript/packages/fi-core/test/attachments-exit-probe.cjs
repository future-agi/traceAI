// One exit-probe case per process (design 8.5). Prints DONE and returns; the process must then exit by itself.
const path = require('node:path');
const { createRequire } = require('node:module');
const load = createRequire(process.env.FI_CORE_PACKAGE_JSON || path.resolve(__dirname, '../package.json'));
const { register, ProjectType } = load('@traceai/fi-core');
const { v1, pcm } = require('./attachments-v1.cjs');
const identity = { traceId: '0123456789abcdef0123456789abcdef', spanId: '0123456789abcdef', traceFlags: 1 };
const blocked = () => new Promise(() => {}); // ignores its abort signal entirely
const provider = (transport, extra = {}) => register({ projectName: 'probe', projectType: ProjectType.OBSERVE, setGlobalTracerProvider: false, spanProcessors: [],
  attachments: { enabled: true, environment: () => ({}), recordAudio: true, transport, ...extra } });
(async () => {
  const name = process.argv[2];
  if (name === 'disabled') {
    const p = register({ projectName: 'probe', projectType: ProjectType.OBSERVE, setGlobalTracerProvider: false, spanProcessors: [] });
    p.getTracer('probe').startSpan('s').end();
    await p.shutdown(); console.log(`DONE ${p.attachments.status().state}`); return;
  }
  if (name === 'enabled-idle') { const p = provider(v1().transport); await p.attachments.ready(); await p.shutdown(); console.log('DONE'); return; }
  if (name === 'enabled-blocked') {
    const p = provider(v1({ putContent: blocked }).transport, { shutdownTimeoutMs: 1000 });
    await p.attachments.ready(); p.attachments.attach(identity, { kind: 'Bytes', data: new Uint8Array(8) }, { mimeType: 'audio/wav', direction: 'mixed' });
    await new Promise(resolve => setImmediate(resolve));
    const result = await p.attachments.shutdown(1000); await p.shutdown();
    if (result.unfinished < 1) throw Error('expected unfinished');
    console.log(`DONE unfinished=${result.unfinished}`); return;
  }
  if (name === 'enabled-no-shutdown') {
    const p = provider(v1({ putContent: blocked }).transport); await p.attachments.ready();
    p.attachments.attach(identity, { kind: 'Bytes', data: new Uint8Array(8) }, { mimeType: 'audio/wav', direction: 'mixed' });
    console.log('DONE'); return; // no shutdown: unref'd handles must not hold the process
  }
  if (name === 'mid-recording') {
    const d = v1(); const p = provider(d.transport, { shutdownTimeoutMs: 10000 }); await p.attachments.ready();
    const w = p.attachments.startRecording(identity, { format: { sampleRateHz: 16000 }, layout: 'mixed', tracks: [{ channels: [0], direction: 'mixed', role: 'mixed' }], operation: 'observe_consumed' });
    for (let i = 0; i < 250; i++) w.tryWrite(pcm(320));
    await p.shutdown();
    const result = await w.finish();
    if (!(d.manifests.length >= 1 || result.state === 'partial')) throw Error(`no manifest: ${result.state}`);
    console.log(`DONE ${result.state} manifests=${d.manifests.length}`); return;
  }
  if (name === 'import-pending') {
    const p = provider(v1().transport); await p.attachments.ready();
    const h = await p.attachments.importCompletedRecording(identity, { provider: 'livekit_egress', providerRef: 'EG_probe' }, { resolver: { resolve: () => new Promise(() => {}) }, timeoutMs: 1000 });
    await p.shutdown();
    if (h.reason !== 'source_unavailable') throw Error(h.reason);
    console.log(`DONE ${h.reason}`); return;
  }
  throw Error(`unknown case ${name}`);
})().catch(error => { console.error(String(error && error.message)); process.exitCode = 1; });
