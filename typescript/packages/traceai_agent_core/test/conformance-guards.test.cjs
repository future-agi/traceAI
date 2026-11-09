const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const vm = require('node:vm');
const { spawnSync } = require('node:child_process');
const { checkShape, checkPrivacy } = require('./conformance/index.cjs');
const attrs = values => Object.entries(values).map(([key, value]) => ({ key, value: typeof value === 'string' ? { stringValue: value } : { intValue: value } }));
function wire() {
  const root = { traceId:'trace', spanId:'root', attributes:attrs({ 'session.id':'session', 'fi.agent.outcome':'error' }) };
  const gen = i => ({ traceId:'trace', spanId:'gen'+i, parentSpanId:'root', startTimeUnixNano:String(i+1), attributes:attrs({ 'fi.span.kind':'LLM', 'gen_ai.request.model':'model', 'gen_ai.usage.input_tokens':10, 'gen_ai.usage.output_tokens':4 }) });
  const tool = { traceId:'trace', spanId:'tool', parentSpanId:'root', status:{code:2}, attributes:attrs({ 'fi.span.kind':'TOOL', 'tool.name':'read', 'fi.agent.outcome':'error' }) };
  return [{resourceSpans:[{scopeSpans:[{spans:[root,gen(0),gen(1),tool]}]}]}];
}
const spans = b => b[0].resourceSpans[0].scopeSpans[0].spans;
const expected = { calls:2, tools:1, outcome:'error', toolOutcome:'error' };
const tokens = (s, n) => { s.attributes.find(a=>a.key==='gen_ai.usage.input_tokens').value.intValue=n; };
test('privacy conserves structure and usage, not just absence of content', () => {
  checkPrivacy(wire(), 'off', expected);
  for (const mutate of [
    b => spans(b).splice(1,2),
    b => spans(b).splice(3,1),
    b => spans(b).slice(1,3).forEach(s => tokens(s,0)),
  ]) { const b=wire(); mutate(b); assert.throws(()=>checkPrivacy(b,'off',expected)); }
});
test('failed tool exported as success fails outcome conformance', () => {
  const b=wire(); checkShape(b,expected);
  spans(b)[3].status.code=1;
  spans(b)[3].attributes.find(a=>a.key==='fi.agent.outcome').value.stringValue='success';
  assert.throws(()=>checkShape(b,expected), /tool outcome|tool status/);
});
test('conserved totals with wrong request attribution fail', () => {
  const b=wire(); tokens(spans(b)[1],20); tokens(spans(b)[2],0);
  assert.throws(()=>checkShape(b,expected), /per-generation/);
});
test('aggregate sources require the last generation, not merely the right total', () => {
  const b=wire(), e={...expected,generationUsage:'last-generation'};
  tokens(spans(b)[1],0); tokens(spans(b)[2],20);
  spans(b)[1].attributes.find(a=>a.key==='gen_ai.usage.output_tokens').value.intValue=0;
  spans(b)[2].attributes.find(a=>a.key==='gen_ai.usage.output_tokens').value.intValue=8;
  checkShape(b,e);
  tokens(spans(b)[1],20); tokens(spans(b)[2],0);
  assert.throws(()=>checkShape(b,e), /per-generation/);
});
test('single-adapter runner propagates a failed report to its process exit', async () => {
  const source=await fs.readFile(path.join(__dirname,'conformance/run.cjs'),'utf8');
  for(const failures of [[],['deliberate dropped generation']]) {
    const process={argv:['node','run.cjs','cursor'],env:{},exitCode:undefined};
    await vm.runInNewContext(source, {process,console:{log(){},error(){}},require(name){
      if(name==='./index.cjs') return {runConformance:async()=>({failures})};
      if(name.startsWith('./drivers/')) return {};
      if(name==='./host-fixture.cjs') return {prepareHost(){}};
      return require(name);
    }});
    assert.equal(process.exitCode,failures.length ? 1 : 0);
  }
});
test('validator propagates independent core, type, and pack failures', async t => {
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'conformance-exit-'));
  t.after(()=>fs.rm(dir,{recursive:true,force:true}));
  await fs.mkdir(path.join(dir,'typescript'));
  const bin=path.join(dir,'bin'); await fs.mkdir(bin);
  for(const [name,body] of Object.entries({
    npm:'exit 0', node:'exit 0', bash:'exit "${SUITE_RC:-0}"',
    pnpm:'case "$1" in --filter) exit "${CORE_RC:-0}";; exec) exit "${TYPES_RC:-0}";; --dir) exit "${PACK_RC:-0}";; *) exit 99;; esac',
  })) await fs.writeFile(path.join(bin,name),'#!/bin/sh\n'+body+'\n',{mode:0o700});
  // Exercise the production validator control flow with only external tools replaced.
  const source=(await fs.readFile(path.join(__dirname,'conformance/validate-e2b.sh'),'utf8')).replaceAll('/work',dir).replaceAll('/opt/Cursor',path.join(dir,'Cursor'));
  const file=path.join(dir,'validate.sh'); await fs.writeFile(file,source);
  for(const [name,code] of [['SUITE_RC',40],['CORE_RC',41],['TYPES_RC',42],['PACK_RC',43],['PASS',0]]) {
    const r=spawnSync('/bin/bash',[file],{env:{PATH:bin+':/usr/bin:/bin',[name]:String(code)},encoding:'utf8'});
    assert.equal(r.status,code,r.stdout+r.stderr);
  }
});
