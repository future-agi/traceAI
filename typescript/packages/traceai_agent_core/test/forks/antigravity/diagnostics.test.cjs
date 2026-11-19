const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const core = require('../../../dist/src');

test('only adapter-allowlisted diagnostics cross setup/status boundaries', async t => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'fi-diagnostics-'));
  const server = http.createServer((req, res) => { req.resume(); res.setHeader('Content-Type','application/json'); res.end('{}'); });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  t.after(async () => {server.closeAllConnections(); await new Promise(r => server.close(r)); await fs.rm(home, {recursive:true, force:true});});
  const options = {agent:'pi', home, env:{}};
  const base = {...options, credentials:{apiKey:'synthetic-api',secretKey:'synthetic-secret'},
    otlpEndpoint:`http://127.0.0.1:${server.address().port}/tracer/v1/traces`};
  const diagnostics = {KNOWN_INSTALL_CONFLICT:'Resolve the known installation conflict.'};
  const adapter = {id:'pi', version:'0.1.0', diagnostics, detect:async()=>({supported:true}),
    install:async()=>({registered:false,detail:'KNOWN_INSTALL_CONFLICT'}),
    inspect:async()=>({registered:false,detail:'KNOWN_INSTALL_CONFLICT'}), uninstall:async()=>({registered:false})};
  const known = [{code:'KNOWN_INSTALL_CONFLICT',action:diagnostics.KNOWN_INSTALL_CONFLICT}];
  assert.deepEqual((await core.setup({...base,adapter})).diagnostics,known);
  assert.deepEqual((await core.inspectStatus({...options,adapter})).diagnostics,known);
  assert.ok((await core.inspectStatus({...options,adapter})).issues.includes('KNOWN_INSTALL_CONFLICT'));
  const thrown = {...adapter,install:async()=>{throw new Error('KNOWN_INSTALL_CONFLICT');},inspect:async()=>{throw new Error('KNOWN_INSTALL_CONFLICT');}};
  assert.deepEqual((await core.setup({...base,adapter:thrown})).diagnostics,known);
  assert.deepEqual((await core.inspectStatus({...options,adapter:thrown})).diagnostics,known);
  for (const secret of ['UNALLOWLISTED_SECRET', 'synthetic-secret from provider stdout', '__proto__']) {
    const unsafe = {...adapter,install:async()=>{throw new Error(secret);},inspect:async()=>({registered:false,detail:secret})};
    const setup = await core.setup({...base,adapter:unsafe});
    const status = await core.inspectStatus({...options,adapter:unsafe});
    assert.equal(setup.code,'INSTALLATION_FAILED');
    assert.deepEqual(setup.diagnostics,[]); assert.deepEqual(status.diagnostics,[]);
    assert.ok(!JSON.stringify({setup,status}).includes(secret));
  }
});
