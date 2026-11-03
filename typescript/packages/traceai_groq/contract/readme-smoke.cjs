const fs = require('node:fs');
const http = require('node:http');
const { createRequire } = require('node:module');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = process.argv[2];
const sdkPath = process.argv[3];
const localRequire = createRequire(path.join(root, 'typescript/packages/traceai_groq/package.json'));
const ts = createRequire(path.join(root, 'typescript/package.json'))('typescript');
const readme = fs.readFileSync(path.join(root, 'typescript/packages/traceai_groq/README.md'), 'utf8');
const snippet = readme.split('```typescript\n').map(x => x.split('```')[0]).find(x => x.startsWith('import { register, ProjectType }'));
assert(snippet);
let payloads = 0;
const server = http.createServer((req, res) => { req.resume(); req.on('end', () => { payloads++; res.end('{}'); }); });
server.listen(0, '127.0.0.1', async () => {
  try {
    process.env.FI_BASE_URL = `http://127.0.0.1:${server.address().port}`;
    process.env.FI_API_KEY = 'synthetic';
    process.env.FI_SECRET_KEY = 'synthetic';
    process.env.GROQ_API_KEY = 'synthetic';
    const fakeFetch = `fetch: async () => new Response(JSON.stringify({ id: 'readme', model: 'synthetic-model', choices: [{index: 0, message: {role:'assistant', content:'hello'}, finish_reason:'stop'}], usage:{prompt_tokens:1, completion_tokens:1,total_tokens:2} }), {headers: {'content-type':'application/json'}})`;
    const code = snippet.replace('new Groq({ apiKey: process.env.GROQ_API_KEY })', `new Groq({ apiKey: process.env.GROQ_API_KEY, ${fakeFetch} })`);
    const compiled = ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop:true } }).outputText;
    const req = name => name === 'groq-sdk' ? require(sdkPath) : localRequire(name);
    await new (Object.getPrototypeOf(async function(){}).constructor)('require', 'exports', compiled)(req, {});
    assert.equal(payloads, 1);
    console.log('README maintained-core setup exported one trace to loopback');
  } catch (error) { console.error(error); process.exitCode = 1; }
  finally { server.close(); }
});
