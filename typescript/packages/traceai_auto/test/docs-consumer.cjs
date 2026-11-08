const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { spawn } = require('node:child_process');
const { createRequire } = require('node:module');
const cwd = path.resolve(process.argv[2]);
const repo = path.resolve(__dirname, '../../../..');
const pkg = path.join(repo, 'typescript/packages/traceai_auto');
const read = name => fs.readFileSync(path.join(pkg, name), 'utf8');
const blocks = text => [...text.matchAll(/```([^\n]*)\n([\s\S]*?)```/g)].map(m => ({lang:m[1],code:m[2]}));
const main = read('README.md');
const frames = blocks(read('docs/FRAMEWORKS.md'));
const config = blocks(read('docs/CONFIGURATION.md'));
const output = [];
const captured = [];
let base;
const server = http.createServer((req,res) => {
  let body=''; req.on('data',b=>body+=b); req.on('end',()=>{
    res.setHeader('content-type','application/json');
    if(req.url==='/tracer/v1/traces') {captured.push(JSON.parse(body));res.end('{}');}
    else if(req.url==='/v1/chat/completions') res.end(JSON.stringify({id:'chatcmpl-docs',object:'chat.completion',created:1,model:'example-model',choices:[{index:0,message:{role:'assistant',content:'Hello.'},finish_reason:'stop'}],usage:{prompt_tokens:7,completion_tokens:2,total_tokens:9}}));
    else {res.statusCode=404;res.end('{}');}
  });
});
const spans = () => captured.flatMap(e=>e.resourceSpans||[]).flatMap(r=>r.scopeSpans||[]).flatMap(s=>s.spans||[]);
const cleanEnv = {...process.env};
for(const key of Object.keys(cleanEnv)) if(key.startsWith('FI_')||key.startsWith('OTEL_')||key==='NODE_OPTIONS') delete cleanEnv[key];
function environment(extra={}) { return {...cleanEnv,FI_API_KEY:'synthetic-fi-api-123',FI_SECRET_KEY:'synthetic-fi-secret-123',FI_BASE_URL:base,FI_PROJECT_NAME:'my-ai-app',OPENAI_API_KEY:'synthetic-provider-123',OPENAI_BASE_URL:base+'/v1',OPENAI_MODEL:'example-model',...extra}; }
function run(command, extra={}, dir=cwd, timeout=120000) {
  return new Promise((resolve,reject)=>{
    const child=spawn('bash',['-c',command],{cwd:dir,env:environment(extra),stdio:['ignore','pipe','pipe']});
    let out='',err=''; child.stdout.on('data',b=>out+=b);child.stderr.on('data',b=>err+=b);
    const timer=setTimeout(()=>{child.kill('SIGKILL');reject(new Error(`timeout: ${command}\n${err}`));},timeout);
    child.on('error',reject);child.on('close',code=>{clearTimeout(timer);code===0?resolve(out):reject(new Error(`exit ${code}: ${command}\n${out}\n${err}`));});
  });
}
function record(test,extra={}) { const result={consumer:path.basename(cwd),test,status:'PASS',...extra};output.push(result);console.log(JSON.stringify(result)); }
function assertSpans(test,{hidden=false,min=1}={}) {
  assert.ok(spans().length>=min,`${test}: ${spans().length} spans`);
  const all=JSON.stringify(captured);
  assert.ok(!all.includes('synthetic-provider-123'));
  if(hidden) {assert.ok(!all.includes('Say hello in one word.'));assert.ok(!all.includes('Hello.'));}
  else assert.ok(all.includes('Hello.'),test+' output missing');
  record(test,{spans:spans().length,names:spans().map(s=>s.name)});
}
function write(name,text) {fs.writeFileSync(path.join(cwd,name),text);}
async function executeSnippets() {
  const app=blocks(main).find(b=>b.lang==='js').code;
  write('app.mjs',app);
  write('app.cjs',app.replace('import OpenAI from "openai";','const OpenAI = require("openai");\n(async () => {')+'\n})();\n');
  const packagePath=path.join(cwd,'package.json'),manifest=JSON.parse(fs.readFileSync(packagePath));
  manifest.scripts={...manifest.scripts,start:'node app.mjs'};fs.writeFileSync(packagePath,JSON.stringify(manifest));
  for(const {code} of blocks(main).filter(b=>b.lang==='sh')) {
    if(code.startsWith('npm install')) continue;
    captured.length=0;
    const command=code.replaceAll('your-api-key','synthetic-fi-api-123').replaceAll('your-secret-key','synthetic-fi-secret-123');
    const result=await run(command);
    if(command.includes('bootstrap')) {assert.ok(JSON.parse(result).integrations.length>0);assert.equal(spans().length,0);record('bootstrap command');}
    else if(command.includes('doctor')) {const report=JSON.parse(result);assert.equal(report.active,true);if(command.includes('--no-check')) {assert.match(report.collector,/not_tested/);assert.equal(spans().length,0);} else {assert.equal(report.collector,'accepted');assert.ok(spans().some(s=>s.name==='traceai.auto.doctor'));}record(command.trim(),{collector:report.collector});}
    else {assert.match(result,/Hello\./);assertSpans(command.trim());}
  }
  captured.length=0;
  const root=fs.readFileSync(path.join(repo, 'README.md'),'utf8').split('## Zero-code tracing for Node.js')[1].split('## Quickstart')[0];
  const rootCommand=blocks(root)[0].code.split('\n').slice(1).join('\n').replaceAll('your-api-key','synthetic-fi-api-123').replaceAll('your-secret-key','synthetic-fi-secret-123');
  await run(rootCommand);assertSpans('root quickstart');
  captured.length=0;
  write('instrument.mjs',config.find(b=>b.lang==='js').code);
  await run(config.find(b=>b.lang==='sh'&&b.code.startsWith('node ')).code,{FI_API_KEY:'',FI_SECRET_KEY:'',OTEL_EXPORTER_OTLP_TRACES_ENDPOINT:base+'/tracer/v1/traces'});
  assertSpans('custom exporter without FI credentials');
  captured.length=0;
  write('futureagi.toml',config.find(b=>b.lang==='toml').code);
  await run('node --import @traceai/auto/register app.mjs');assertSpans('documented TOML hides content',{hidden:true});
  fs.unlinkSync(path.join(cwd,'futureagi.toml'));
  for(const [name,index,command,entry] of [
    ['esbuild',2,'node build.mjs','dist/server.mjs'],
    ['vite',3,'node node_modules/vite/bin/vite.js build','dist/server.mjs'],
    ['rollup',4,'node node_modules/rollup/dist/bin/rollup -c','dist/server.mjs'],
    ['webpack',5,'node webpack-build.cjs','dist/server.cjs'],
  ]) {
    captured.length=0;fs.rmSync(path.join(cwd,'dist'),{recursive:true,force:true});
    const filename={esbuild:'build.mjs',vite:'vite.config.mjs',rollup:'rollup.config.mjs',webpack:'webpack.config.mjs'}[name];
    write(filename,frames[index].code);
    if(name==='webpack')write('webpack-build.cjs',`const webpack=require('webpack');import('./webpack.config.mjs').then(({default:c})=>webpack(c,(err,stats)=>{if(err||stats.hasErrors()){console.error(err||stats.toString());process.exitCode=1;}}));`);
    await run(command);await run(`node ${entry}`);assertSpans('documented '+name+' recipe');
  }
  captured.length=0;write('lambda.mjs',frames[6].code);
  write('invoke.mjs',`import {handler} from './lambda.mjs';\nfor(let i=0;i<2;i++){const result=await handler({},{awsRequestId:'synthetic-request-'+i,functionName:'readme-example',getRemainingTimeInMillis:()=>5000});if(result.body!=='Hello.')throw new Error('bad response');}\n`);
  await run('node invoke.mjs');assertSpans('documented warm Lambda handler',{min:4});
  const req=createRequire(path.join(cwd,'package.json'));
  const auto=path.resolve(path.dirname(req.resolve('@traceai/auto')),'../..');
  const {resolveConfig}=req(path.join(auto,'dist/src/config.js'));
  const defaults=resolveConfig({configFile:'/missing'},{});
  assert.equal(defaults.sampleRate,1);assert.equal(defaults.wireCapture,true);assert.equal(defaults.wireMaxBodyBytes,4194304);assert.equal(defaults.baseUrl,'https://api.futureagi.com');assert.equal(defaults.projectType,'observe');assert.equal(defaults.debug,false);assert.equal(defaults.mastraSpanProducer,'exporter');assert.equal(defaults.sdkDisabled,false);
  assert.deepEqual(defaults.instrumentations,[]);assert.deepEqual(defaults.disabledInstrumentations,[]);
  assert.equal(resolveConfig({}, {FI_WIRE_CAPTURE:'1'}).wireCapture,false);
  record('configuration defaults against installed package');
  captured.length=0;
  const {patchFetch}=req('@traceai/auto/edge');let edge;
  const hook=patchFetch(4194304,result=>edge=result);
  await (await fetch(base+'/v1/chat/completions',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({model:'example-model',messages:[{role:'user',content:'hello'}]})})).text();
  await Promise.all([...hook.pending]);hook.remove();
  assert.ok(edge);assert.equal(spans().length,0);record('edge observer has no implicit exporter');
}
async function nextRecipe() {
  const dir=path.join(cwd,'next-example');fs.mkdirSync(path.join(dir,'app/api/chat'),{recursive:true});
  fs.writeFileSync(path.join(dir,'instrumentation.ts'),frames[0].code);
  fs.writeFileSync(path.join(dir,'next.config.mjs'),frames[1].code);
  fs.writeFileSync(path.join(dir,'package.json'),JSON.stringify({name:'readme-next-example',private:true}));
  fs.writeFileSync(path.join(dir,'app/layout.js'),`export default function Layout({children}) {return <html><body>{children}</body></html>;}`);
  fs.writeFileSync(path.join(dir,'app/page.js'),`export default function Page(){return <main>TraceAI documentation example</main>;}`);
  fs.writeFileSync(path.join(dir,'app/api/chat/route.js'),`import OpenAI from 'openai';export const runtime='nodejs';export const dynamic='force-dynamic';export async function GET(){const result=await new OpenAI().chat.completions.create({model:process.env.OPENAI_MODEL,messages:[{role:'user',content:'Say hello in one word.'}]});return Response.json({message:result.choices[0].message.content});}`);
  await run('node ../node_modules/next/dist/bin/next build',{NEXT_TELEMETRY_DISABLED:'1'},dir,240000);
  captured.length=0;
  const child=spawn(process.execPath,['../node_modules/next/dist/bin/next','start','-p','3456'],{cwd:dir,env:environment({NEXT_TELEMETRY_DISABLED:'1'})});
  let logs='';child.stdout.on('data',b=>logs+=b);child.stderr.on('data',b=>logs+=b);
  try {
    let ready=false;
    for(let i=0;i<120;i++){try {const r=await fetch('http://127.0.0.1:3456/');if(r.ok){ready=true;break;}}catch{}await new Promise(r=>setTimeout(r,250));}
    assert.ok(ready,logs);
    const response=await fetch('http://127.0.0.1:3456/api/chat');assert.equal(response.status,200,logs);assert.equal((await response.json()).message,'Hello.');
    for(let i=0;i<80&&!spans().length;i++)await new Promise(r=>setTimeout(r,250));
    assertSpans('documented Next production build/start');
  } finally {child.kill('SIGTERM');await new Promise(resolve=>child.once('close',resolve));}
}
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));base=`http://127.0.0.1:${server.address().port}`;
  await executeSnippets();
  await nextRecipe();
  if (process.argv[3]) { fs.mkdirSync(process.argv[3],{recursive:true}); fs.writeFileSync(path.join(process.argv[3],path.basename(cwd)+'.json'),JSON.stringify(output,null,2)); }
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>{server.closeAllConnections();server.close();});
