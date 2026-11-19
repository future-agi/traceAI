import {OpenCode} from '@opencode/client';
import {spawn} from 'node:child_process';
import {writeFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
export async function scenarios({binary,env,project,captures}) {
 env.OPENCODE_PASSWORD='synthetic-local-test-only';
 const host=spawn(binary,['serve','--hostname','127.0.0.1','--port','43107'],{cwd:project,env,stdio:['ignore','pipe','pipe']});
 let logs='';host.stdout.on('data',b=>logs+=b);host.stderr.on('data',b=>logs+=b);
 const client=OpenCode.make({baseUrl:'http://127.0.0.1:43107',headers:{Authorization:`Basic ${Buffer.from(`opencode:${env.OPENCODE_PASSWORD}`).toString('base64')}`}});
 const pause=ms=>new Promise(r=>setTimeout(r,ms));
 const all=()=>captures.flatMap(r=>r.resourceSpans.flatMap(rs=>rs.scopeSpans.flatMap(s=>s.spans)));
 const attrs=s=>Object.fromEntries(s.attributes.map(a=>[a.key,Object.values(a.value)[0]]));
 try {
  for(let n=0;;n++){try{await client.server.info({signal:AbortSignal.timeout(1000)});break;}catch(e){if(n>=100)throw e;await pause(100);}}
  const before=all().length;
  const session=await client.session.create({location:{directory:project},title:'Directional privacy review',model:{id:'test',providerID:'mock'}});
  for(const text of ['Read sample.txt and summarize it.','Explain the previous answer again.']){
   await client.session.prompt({sessionID:session.id,text});await client.experimental.session.wait({sessionID:session.id},{signal:AbortSignal.timeout(30000)});await pause(1500);
  }
  const spans=all().slice(before);
  const inputs=spans.map(attrs).map(a=>a['input.value']).filter(Boolean);
  const result={hostVersion:'2.2.5',setting:'FI_HIDE_OUTPUTS=true',roots:spans.filter(s=>!s.parentSpanId).length,spans:spans.length,explicitOutputs:spans.map(attrs).filter(a=>a['output.value']!==undefined).length,assistantOutputLeaked:inputs.some(x=>x.includes('The sample file contains public text.')),toolOutputLeaked:inputs.some(x=>x.includes('Public sample file.'))};
  await writeFile('/work/evidence/privacy-native-result.json',JSON.stringify(result,null,2));
  assert.equal(result.roots,2);assert.equal(result.explicitOutputs,0);
  assert.equal(result.assistantOutputLeaked,false,'hidden model output must not appear in later input');
  assert.equal(result.toolOutputLeaked,false,'hidden tool output must not appear in later input');
 }finally{host.kill('SIGTERM');await Promise.race([new Promise(r=>host.once('exit',r)),pause(3000)]);if(host.exitCode===null&&host.signalCode===null)host.kill('SIGKILL');await writeFile('/work/evidence/privacy-server.log',logs.replace(/server password .*/g,'server password [REDACTED]'));}
}
