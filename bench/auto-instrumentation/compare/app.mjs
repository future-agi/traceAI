import { createHash } from 'node:crypto';
// INSTRUMENTATION_SETUP
const scenario = process.env.BENCH_SCENARIO;
const base = process.env.BENCH_PROVIDER;
const key = process.env.OPENAI_API_KEY;
const model = 'gpt-4o-mini';
let client, ai;
if (scenario === 'anthropic_stream') {
  const {default: Anthropic} = await import('@anthropic-ai/sdk');
  client = new Anthropic({apiKey:key, baseURL:base, maxRetries:0, timeout:10000});
} else if (scenario === 'gemini') {
  const {GoogleGenAI} = await import('@google/genai');
  client = new GoogleGenAI({apiKey:key, httpOptions:{baseUrl:base, apiVersion:'v1beta'}});
} else if (scenario === 'vercel') {
  ai = await import('ai');
  const {createOpenAI} = await import('@ai-sdk/openai');
  client = createOpenAI({apiKey:key, baseURL:base + '/v1'}).chat(model);
} else if (scenario === 'unsupported_sdk') {
  const {default: Together} = await import('together-ai');
  client = new Together({apiKey:key, baseURL:base + '/v1', maxRetries:0, timeout:10000});
} else if (scenario !== 'raw_http') {
  const {default: OpenAI} = await import('openai');
  client = new OpenAI({apiKey:key, baseURL:base + '/v1', maxRetries:0, timeout:10000});
}
let invoke = async function invoke(prompt) {
  let first = null, firstChunk = null;
  if (scenario === 'anthropic_stream') {
    const chunks = [];
    const stream = await client.messages.create({model:'claude-sonnet-4-5', max_tokens:20, messages:[{role:'user', content:prompt}], stream:true});
    for await (const chunk of stream) {
      if (firstChunk === null) firstChunk = process.hrtime.bigint();
      if (chunk.delta?.text && first === null) first = process.hrtime.bigint();
      chunks.push(chunk);
    }
    return [{chunks}, first, firstChunk];
  }
  if (scenario === 'gemini') return [await client.models.generateContent({model:'gemini-2.0-flash', contents:prompt}), first, firstChunk];
  if (scenario === 'embeddings') return [await client.embeddings.create({model:'text-embedding-3-small', input:prompt, encoding_format:'float'}), first, firstChunk];
  if (scenario === 'transcription') {
    const {toFile} = await import('openai');
    const file = await toFile(Buffer.from(process.env.BENCH_MEDIA, 'base64'), 'compare.wav', {type:'audio/wav'});
    return [await client.audio.transcriptions.create({model:'whisper-1', file, response_format:'verbose_json'}), first, firstChunk];
  }
  if (scenario === 'tts') {
    const response = await client.audio.speech.create({model:'tts-1', input:prompt, voice:'alloy', response_format:'wav'});
    const body = Buffer.from(await response.arrayBuffer());
    return [{bytes:body.length, sha256:createHash('sha256').update(body).digest('hex')}, first, firstChunk];
  }
  if (scenario === 'vercel') {
    const result = await ai.generateText({model:client, prompt, maxRetries:0});
    return [{text:result.text, finishReason:result.finishReason, usage:result.usage, toolCalls:result.toolCalls}, first, firstChunk];
  }
  if (scenario === 'raw_http') {
    const response = await fetch(base + '/v1/chat/completions', {method:'POST', headers:{'Content-Type':'application/json', Authorization:'Bearer ' + key},
      body:JSON.stringify({model, messages:[{role:'user', content:prompt}]})});
    if (!response.ok) throw new Error('unexpected HTTP ' + response.status);
    return [await response.json(), first, firstChunk];
  }
  const kwargs = {model:scenario === 'openai_error' ? 'compare-error' : model, messages:[{role:'user', content:prompt}]};
  if (scenario === 'tool_calls') kwargs.tools = [{type:'function', function:{name:'lookup_weather', description:'Look up weather',
    parameters:{type:'object', properties:{city:{type:'string'}}, required:['city']}}}];
  if (scenario === 'openai_stream') {
    const chunks = [];
    const stream = await client.chat.completions.create({...kwargs, stream:true, stream_options:{include_usage:true}});
    for await (const chunk of stream) {
      if (firstChunk === null) firstChunk = process.hrtime.bigint();
      if (chunk.choices?.[0]?.delta?.content && first === null) first = process.hrtime.bigint();
      chunks.push(chunk);
    }
    return [{chunks}, first, firstChunk];
  }
  try { return [await client.chat.completions.create(kwargs), first, firstChunk]; }
  catch (error) {
    if (scenario !== 'openai_error') throw error;
    return [{error_status:error.status ?? null, body:error.error ?? null}, first, firstChunk];
  }
};
// MANUAL_WRAPPER
console.log(JSON.stringify({kind:'ready'}));
for (let i=0; i<Number(process.env.BENCH_N)+Number(process.env.BENCH_WARMUP); i++) {
  const epochStart = (performance.timeOrigin + performance.now())*1e6;
  const start = process.hrtime.bigint();
  const [response, first, firstChunk] = await invoke('compare-input-canary');
  const end = process.hrtime.bigint();
  const epochEnd = (performance.timeOrigin + performance.now())*1e6;
  console.log(JSON.stringify({kind:'call', i, start_unix_ns:epochStart, end_unix_ns:epochEnd, span_clock_tolerance_ns:1000000, warmup:i<Number(process.env.BENCH_WARMUP),
    latency_us:Number(end-start)/1000, ttft_us:first===null ? null : Number(first-start)/1000,
    ttfc_us:firstChunk===null ? null : Number(firstChunk-start)/1000,
    first_text_unix_ns:first===null ? null : epochStart+Number(first-start),
    first_chunk_unix_ns:firstChunk===null ? null : epochStart+Number(firstChunk-start), response},
    (k,v)=>typeof v === 'bigint' ? v.toString() : v));
  // Outside call timers: separate epochs despite millisecond export timestamps.
  await new Promise(resolve=>setTimeout(resolve, 4));
}
await new Promise(resolve=>setTimeout(resolve, Number(process.env.BENCH_SETTLE)*1000));
// FLUSH_SETUP
console.log(JSON.stringify({kind:'done'}));
