const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const {spawn} = require('node:child_process');
const delay = ms => new Promise(r=>setTimeout(r,ms));
const attrs = s => Object.fromEntries(s.attributes.map(a=>[a.key,Object.values(a.value)[0]]));
exports.run = async ({pi,cwd,env,spans,requests}) => {
  const evidence=[];
  for (const kind of ['follow_up', 'steer']) for (const mode of ['one-at-a-time','all']) {
    const offset=spans().length, reqOffset=requests.length;
    const p=spawn(pi,['--offline','--no-approve','--mode','rpc','--provider','mock','--model','test','--session-id',`review-queue-${kind}-${mode}`],{cwd,env,stdio:['pipe','pipe','pipe']});
    let text='',err=''; const events=[];
    p.stdout.on('data',b=>{text+=b;let n;while((n=text.indexOf('\n'))>=0){const line=text.slice(0,n);text=text.slice(n+1);if(line)events.push(JSON.parse(line));}});
    p.stderr.on('data',b=>err+=b);
    const send=x=>p.stdin.write(JSON.stringify(x)+'\n');
    const wait=async predicate=>{for(let i=0;i<400;i++){if(predicate())return;await delay(25);}throw Error('queue timeout '+JSON.stringify(events).slice(-2000)+err);};
    try {
      send({type:kind === 'steer' ? 'set_steering_mode' : 'set_follow_up_mode',id:'mode',mode});
      await wait(()=>events.some(e=>e.type==='response'&&e.id==='mode'));
      send({type:'prompt',id:'first',message:'REVIEW_QUEUE_INITIAL'});
      await wait(()=>requests.length>reqOffset);
      send({type:kind,id:'second',message:'REVIEW_QUEUE_SECOND'});
      send({type:kind,id:'third',message:'REVIEW_QUEUE_THIRD'});
      await wait(()=>events.some(e=>e.type==='response'&&e.id==='second')&&events.some(e=>e.type==='response'&&e.id==='third'));
      await wait(()=>events.some(e=>e.type==='agent_settled'));
      p.stdin.end();
      await new Promise((r,j)=>{p.on('exit',r);p.on('error',j);});
      const observed=spans().slice(offset);
      const users=events.filter(e=>e.type==='message_end'&&e.message?.role==='user').map(e=>e.message.content);
      const roots=observed.filter(s=>!s.parentSpanId);
      assert.equal(users.length,3,'three submitted prompts actually delivered by host');
      assert.equal(roots.length, 3, `${kind}/${mode}: every delivered user has a root`);
      assert.equal(observed.filter(s=>attrs(s)['fi.span.kind']==='LLM').length, requests.length-reqOffset);
      assert(roots.every(s=>attrs(s)['gen_ai.usage.total_tokens']===undefined));
      if(mode==='all') {
        const second=roots.find(s=>attrs(s)['input.value']==='REVIEW_QUEUE_SECOND');
        const third=roots.find(s=>attrs(s)['input.value']==='REVIEW_QUEUE_THIRD');
        assert.equal(second.links[0].traceId,third.traceId);
      }
      evidence.push({kind,mode,users,roots:roots.map(s=>({traceId:s.traceId,attributes:attrs(s)})),providerRequests:requests.slice(reqOffset),spans:observed,queueResponses:events.filter(e=>e.type==='response')});
    } finally {if(p.exitCode===null)p.kill('SIGKILL');}
  }
  await fs.writeFile('/work/review-queue.json',JSON.stringify(evidence,null,2));
  console.log('REVIEW_QUEUE '+JSON.stringify(evidence.map(x=>({kind:x.kind,mode:x.mode,deliveredUsers:x.users.length,roots:x.roots.length,rootInputs:x.roots.map(r=>r.attributes['input.value']),requests:x.providerRequests.length}))));
};
