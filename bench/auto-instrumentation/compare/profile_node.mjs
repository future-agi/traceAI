// App prefix is inserted above this runner in scratch, preserving native hooks.
import {Session as ProfileSession} from 'node:inspector';
import {writeFileSync as profileWrite} from 'node:fs';
import {createRequire as profileRequireFactory} from 'node:module';
import {PerformanceObserver as ProfileObserver} from 'node:perf_hooks';
const profileRequire = profileRequireFactory(import.meta.url);
const profileTool = process.env.PROFILE_TOOL;
const profilePolicy = process.env.PROFILE_PROCESSOR;
const profileState = globalThis[Symbol.for('traceai.auto.agent.v1')];
if (profileTool === 'traceai') {
  if (!profileState?.active) throw new Error('traceAI auto entry did not initialize');
  if (profilePolicy === 'batch') {
    const {BatchSpanProcessor} = profileRequire(process.env.PROFILE_NODE_DEPS + '/node_modules/@opentelemetry/sdk-trace-base');
    const old = profileState.processor;
    await old.forceFlush();
    // Experiment only: retain the agent's exact guarded/privacy exporter and
    // coverage onStart; use the published OTel BSP, not a custom fake queue.
    const replacement = new BatchSpanProcessor(old._exporter, {scheduledDelayMillis:200, maxQueueSize:2048, maxExportBatchSize:512});
    replacement.onStart = old.onStart;
    const list = profileState.provider._activeSpanProcessor._spanProcessors;
    const index = list.indexOf(old);
    if (index < 0) throw new Error('Cannot locate agent processor; refusing silent no-op');
    list[index] = replacement;
    profileState.processor = replacement;
  }
}
const profileMode = process.env.PROFILE_MODE;
const profileN = Number(process.env.BENCH_N);
for (let i=0; i<Number(process.env.BENCH_WARMUP); i++) await invoke('compare-input-canary');
const profileTimes = {}, profileCounts = {}, profileStack = [], profileRestore = [];
function profilePatch(object, name, stage) {
  const old = object?.[name];
  if (typeof old !== 'function') return;
  object[name] = function(...args) {
    const item = {start:process.hrtime.bigint(), child:0}; profileStack.push(item);
    try { return Reflect.apply(old,this,args); }
    finally {
      const elapsed = Number(process.hrtime.bigint()-item.start)/1000;
      profileStack.pop();
      profileTimes[stage] = (profileTimes[stage]??0) + Math.max(0,elapsed-item.child);
      profileCounts[stage] = (profileCounts[stage]??0)+1;
      if (profileStack.length) profileStack.at(-1).child += elapsed;
    }
  };
  profileRestore.push(()=>{object[name]=old;});
}
let profileGC = 0;
const profileObserver = new ProfileObserver(list=>{for(const event of list.getEntries()) profileGC += event.duration*1000;});
profileObserver.observe({entryTypes:['gc']});
if (profileMode === 'internal') {
  profilePatch(JSON,'parse','json_codec_all'); profilePatch(JSON,'stringify','json_codec_all');
  profilePatch(Buffer,'from','body_copy_all'); profilePatch(Buffer,'concat','body_copy_all');
  profilePatch(TextEncoder.prototype,'encode','body_copy_all');
  profilePatch(TextDecoder.prototype,'decode','body_copy_all');
  if (profileState) {
    profilePatch(profileState.processor,'onEnd','processor_export');
    profilePatch(profileState.processor._exporter,'export','privacy_and_export_dispatch');
    const tracer = profileState.provider.getTracer('@traceai/auto','0.1.0');
    profilePatch(tracer,'startSpan','span');
    profilePatch(tracer,'getSpanLimits','span');
  }
}
if (profileMode === 'cpu') {
  // Untimed named shims make otherwise opaque builtins visible to the sampling
  // profiler. Only samples with a real instrumentation ancestor are attributed.
  const parse = JSON.parse, stringify = JSON.stringify;
  JSON.parse = function profileJSONParse(...args) { return Reflect.apply(parse,this,args); };
  JSON.stringify = function profileJSONStringify(...args) { return Reflect.apply(stringify,this,args); };
  profileRestore.push(()=>{JSON.parse=parse;JSON.stringify=stringify;});
  for (const [object,name] of [[Buffer,'from'],[Buffer,'concat'],[TextEncoder.prototype,'encode'],[TextDecoder.prototype,'decode']]) {
    const old=object[name];
    object[name]=function profileBodyCopy(...args) { return Reflect.apply(old,this,args); };
    profileRestore.push(()=>{object[name]=old;});
  }
}
const profileSession = new ProfileSession();
const profilePost = (name,args={})=>new Promise((resolve,reject)=>profileSession.post(name,args,(error,result)=>error?reject(error):resolve(result)));
if (profileMode === 'cpu') {
  profileSession.connect();
  await profilePost('Profiler.enable');
  await profilePost('Profiler.setSamplingInterval',{interval:100});
  await profilePost('Profiler.start');
}
console.log(JSON.stringify({kind:'ready'}));
const profileCalls = [];
const profileCPU = process.cpuUsage();
for (let i=0; i<profileN; i++) {
  const start = process.hrtime.bigint(), cpu = process.cpuUsage();
  const [response] = await invoke('compare-input-canary');
  const elapsed = Number(process.hrtime.bigint()-start)/1000, used = process.cpuUsage(cpu);
  profileCalls.push({kind:'call',i,latency_us:elapsed,cpu_us:used.user+used.system,response});
  // Keep inter-call drain/gap outside latency; CPU profile includes it and
  // marks idle/unattributed separately instead of treating it as agent CPU.
  await new Promise(resolve=>setTimeout(resolve,4));
}
const profileUsed = process.cpuUsage(profileCPU);
if (profileMode === 'cpu') {
  const {profile} = await profilePost('Profiler.stop');
  profileWrite(process.env.PROFILE_RAW,JSON.stringify(profile));
  await profilePost('Profiler.disable'); profileSession.disconnect();
}
for (const restore of profileRestore.reverse()) restore();
await new Promise(resolve=>setImmediate(resolve));
profileObserver.disconnect();
for (const call of profileCalls) console.log(JSON.stringify(call));
console.log(JSON.stringify({kind:'profile',processor:profileTool==='traceai'?profilePolicy:'native',
  cpu_us_per_call:(profileUsed.user+profileUsed.system)/profileN,
  stages_us:Object.fromEntries(Object.entries(profileTimes).map(([k,v])=>[k,v/profileN])),
  stage_calls_per_call:Object.fromEntries(Object.entries(profileCounts).map(([k,v])=>[k,v/profileN])),
  gc_us_per_call:profileGC/profileN}));
await new Promise(resolve=>setTimeout(resolve,Number(process.env.BENCH_SETTLE)*1000));
// FLUSH_SETUP
if (profileState) await profileState.processor.forceFlush();
console.log(JSON.stringify({kind:'done'}));
