const assert = require('node:assert/strict');
const http = require('node:http');
const vscode = require('vscode');
const { setTimeout: delay } = require('node:timers/promises');
const request = (port, path, body) => new Promise((resolve, reject) => {
  const req = http.request({hostname:'127.0.0.1',port,path,method:body ? 'POST' : 'GET',headers:{'Content-Type':'application/json'}}, res => {
    const chunks=[];res.on('data',c=>chunks.push(c));res.on('end',()=>resolve({status:res.statusCode,body:Buffer.concat(chunks).toString()}));
  });req.on('error',reject);req.end(body);
});
async function suite() {
  console.log('ELECTRON_SUITE_START');
  const extension = vscode.extensions.getExtension('future-agi.vscode-extension');
  assert.ok(extension); await extension.activate(); assert.equal(extension.isActive,true);
  console.log('ELECTRON_ACTIVATED', JSON.stringify({ trusted:vscode.workspace.isTrusted, remote:!!vscode.env.remoteName }));
  const commands = await vscode.commands.getCommands(true);
  assert.ok(commands.includes('futureagi.tracing.status')); assert.ok(commands.includes('futureagi.tracing.toggle'));
  const body=JSON.stringify({resourceSpans:[{resource:{attributes:[]},scopeSpans:[{scope:{name:'github.copilot',version:'synthetic'},spans:[{
    traceId:'12345678901234567890123456789012ab',spanId:'12345678901234ab',name:'chat synthetic',kind:1,
    startTimeUnixNano:'1234567890000000000',endTimeUnixNano:'1234567890100000000',
    attributes:[{key:'gen_ai.operation.name',value:{stringValue:'chat'}},{key:'gen_ai.input.messages',value:{stringValue:'private-content'}}]
  }]}]}]});
  const send=()=>request(process.env.FI_TEST_RELAY_PORT,`/${process.env.FI_TEST_RELAY_TOKEN}/v1/traces`,body);
  const count=async()=>JSON.parse((await request(process.env.FI_TEST_MOCK_PORT,'/counts')).body).count;
  let started=false;
  for(let i=0;i<100;i++){try{assert.equal((await send()).status,200);started=true;break;}catch{await delay(100);}}
  assert.ok(started,'extension relay did not start');
  for(let i=0;i<100 && await count()<1;i++) await delay(100);
  assert.equal(await count(),1);
  await vscode.commands.executeCommand('futureagi.tracing.toggle');
  assert.equal((await send()).status,200); await delay(300); assert.equal(await count(),1);
  await vscode.commands.executeCommand('futureagi.tracing.toggle');
  assert.equal((await send()).status,200);
  for(let i=0;i<100 && await count()<2;i++) await delay(100);
  assert.equal(await count(),2);
  console.log('ELECTRON_EXTENSION_ACTIVATION_PASS');
}
exports.run = async () => {
  try { await suite(); }
  catch(error) {
    console.error('ELECTRON_ASSERTION_FAILED', error.stack);
    await require('node:fs/promises').writeFile('/work/electron-failure.json', JSON.stringify({message:error.message, stack:error.stack}));
    throw error;
  }
};
