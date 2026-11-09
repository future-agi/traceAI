// E2B-only integration entrypoint. Requires xvfb-run and an isolated synthetic HOME.
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const net = require('node:net');
const crypto = require('node:crypto');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { runTests } = require('@vscode/test-electron');
const { setup, updateJson } = require('@traceai/agent-core');
const { bootstrapPath, installationPath, synchronize } = require('../dist');
(async () => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'traceai-vscode-electron-'));
  let server;
  try {
    const received = [];
    server = http.createServer(async (req, res) => {
      if (req.url === '/counts') { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify({ count: received.length })); return; }
      const chunks = []; for await (const c of req) chunks.push(c);
      const body = Buffer.concat(chunks);
      if (body.toString() !== '{"resourceSpans":[]}') {
        if (req.headers['x-api-key'] !== 'synthetic-api' || req.headers['x-secret-key'] !== 'synthetic-secret' || req.headers['x-project-name'] !== 'synthetic-editor') throw new Error('HEADER_ASSERTION_FAILED');
        if (body.includes('private-content')) throw new Error('PRIVACY_ASSERTION_FAILED');
        // VS Code itself emits startup timing spans. Count only our replay, not native background traffic.
        if (body.includes('12345678901234567890123456789012ab')) received.push(body.length);
      }
      res.setHeader('content-type', req.headers['content-type'] || 'application/json');
      res.end(req.headers['content-type'] === 'application/x-protobuf' ? Buffer.alloc(0) : '{}');
    });
    await new Promise(r => server.listen(0, '127.0.0.1', r));
    const env = { FI_AGENT_CONFIG_DIR: path.join(home, 'custom-config'), XDG_STATE_HOME: path.join(home, 'custom-state') };
    await setup({ agent:'copilot-vscode', home, env, configureOnly:true, scope:'user', projectName:'synthetic-editor', captureContent:false,
      credentials:{apiKey:'synthetic-api',secretKey:'synthetic-secret'}, otlpEndpoint:`http://127.0.0.1:${server.address().port}/v1/traces`, approveDestination:true });
    const socket = net.createServer(); await new Promise(r => socket.listen(0, '127.0.0.1', r));
    const port = socket.address().port; await new Promise(r => socket.close(r));
    const id = crypto.randomBytes(16).toString('hex'), token = crypto.randomBytes(32).toString('hex');
    const settingsPath = path.join(home, 'editor', 'User', 'settings.json');
    await updateJson(installationPath({home,env}), () => ({ version:1, id, token, port, settingsPath, scope:'user', channel:'stable',
      cli:'/synthetic/code', extensionOwned:true, applied:false, captureContent:false }));
    await updateJson(bootstrapPath(home), () => ({version:1,id,configDir:env.FI_AGENT_CONFIG_DIR,stateDir:path.join(env.XDG_STATE_HOME,'futureagi','agents')}));
    await synchronize(installationPath({home,env}), true, false);
    const extracted = path.join(home, 'extracted'); await fs.mkdir(extracted);
    await promisify(execFile)('unzip', ['-q', path.resolve(__dirname,'../extension.vsix'), '-d', extracted]);
    await runTests({ version: process.env.FI_TEST_VSCODE_VERSION || '1.143.1',
      extensionDevelopmentPath:path.join(extracted,'extension'), extensionTestsPath:path.join(__dirname,'electron-suite.cjs'),
      extensionTestsEnv:{ HOME:home, FI_TEST_RELAY_PORT:String(port), FI_TEST_RELAY_TOKEN:token,
        FI_TEST_MOCK_PORT:String(server.address().port), FI_TEST_CORE:require.resolve('@traceai/agent-core'),
        FI_TEST_CONFIG_DIR:env.FI_AGENT_CONFIG_DIR },
      launchArgs:['--no-sandbox','--disable-gpu','--disable-workspace-trust','--skip-welcome','--skip-release-notes',
        '--user-data-dir',path.join(home,'editor'),'--extensions-dir',path.join(home,'extensions')] });
    console.log(JSON.stringify({ activation:'PASS', forwarded:received.length, syntheticOnly:true, vsix:'PASS' }));
  } finally {
    if (server) { server.closeAllConnections(); await new Promise(r => server.close(r)); }
    await fs.rm(home, {recursive:true,force:true});
  }
})().catch(error => { console.error(error.message === 'Failed to download and unzip VS Code' ? 'VSCODE_DOWNLOAD_FAILED' : String(error)); process.exitCode=1; });
