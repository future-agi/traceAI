const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const {join} = require('node:path');
const {tmpdir} = require('node:os');
const {resolveConfig} = require('@traceai/agent-core');
const {SemanticConventions: SC} = require('@traceai/fi-semantic-conventions');
const {normalizeSpan} = require('../dist/normalize.js');
const {nativePath} = require('../dist/adapter.js');
const config = {projectName:'test', captureContent:true, hideInputs:false, hideOutputs:false, hideErrorDetails:false};
function span(attributes) {
  return {name:'openclaw.model.call', attributes, status:{code:1},
    spanContext:()=>({traceId:'a'.repeat(32),spanId:'b'.repeat(16),traceFlags:1})};
}

test('cache details use canonical backend aliases without adding cached tokens to totals', () => {
  const attrs = normalizeSpan(span({
    'openclaw.model_call.observation_unit':'request',
    'gen_ai.usage.input_tokens':11, 'gen_ai.usage.output_tokens':7,
    'gen_ai.usage.cache_read.input_tokens':5, 'gen_ai.usage.cache_creation.input_tokens':2,
    'openclaw.model_call.time_to_first_byte_ms':12,
  }),config).attributes;
  assert.equal(attrs[SC.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ],5);
  assert.equal(attrs[SC.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE],2);
  assert.equal(attrs[SC.LLM_TOKEN_COUNT_PROMPT],11);
  assert.equal(attrs[SC.LLM_TOKEN_COUNT_TOTAL],18);
  assert.equal(attrs[SC.FI_USAGE_SCOPE],'request');
  assert.equal(attrs[SC.FI_USAGE_CONTRIBUTION],'additive');
  assert.equal(attrs[SC.FI_AGENT_FIDELITY],'native-request');
  assert.equal(attrs['gen_ai.usage.cache_read.input_tokens'],undefined);
  assert.equal(attrs['gen_ai.usage.cache_creation.input_tokens'],undefined);
  assert.equal(attrs[SC.GEN_AI_SERVER_TIME_TO_FIRST_TOKEN],undefined,'first byte is not necessarily first token');
  assert.equal(attrs[SC.GEN_AI_COST_TOTAL],undefined,'no cost in native trace attributes');
});

test('unified core redacts secret aliases and nested structured values before truncation', () => {
  const secret='synthetic-application-credential';
  for(const key of ['secret_key','secret-key','secretKey','SecretKey']) {
    for(const value of [JSON.stringify({[key]:secret}),`${key}=${secret}`,`${key}="${secret}"`,
      JSON.stringify({nested:JSON.stringify({[key]:secret})}),
      JSON.stringify({[key]:{padding:'x'.repeat(21000),value:secret}})]) {
      const attrs=normalizeSpan(span({'openclaw.content.output_messages':value}),config).attributes;
      assert.ok(!attrs['output.value'].includes(secret));
      assert.ok(attrs['output.value'].includes('[REDACTED]'));
      assert.ok(attrs['output.value'].length<=20000);
    }
  }
});

test('tool failure envelopes respect error-detail privacy even when the host span succeeded', () => {
  for (const flag of [{isError:true},{is_error:true},{success:false},{type:'error'},{status:'failed'}]) {
    const source={...span({'openclaw.content.tool_output':JSON.stringify({...flag,message:'private error body'})}),name:'openclaw.tool.execution'};
    const hidden=normalizeSpan(source,{...config,hideErrorDetails:true});
    assert.equal(hidden.status.code,2);
    assert.equal(hidden.attributes['output.value'],undefined);
    const visible=normalizeSpan(source,config);
    assert.equal(visible.status.code,2);
    assert.match(visible.attributes['output.value'],/private error body/);
  }
});

test('OpenClaw native config and preferences share override precedence and home expansion', async t => {
  const home=await fs.mkdtemp(join(tmpdir(),'fi-openclaw-paths-'));
  t.after(()=>fs.rm(home,{recursive:true,force:true}));
  for(const [env, expected] of [
    [{},join(home,'.openclaw','openclaw.json')],
    [{OPENCLAW_HOME:join(home,'custom-home')},join(home,'custom-home','.openclaw','openclaw.json')],
    [{OPENCLAW_HOME:'~/custom-home'},join(home,'custom-home','.openclaw','openclaw.json')],
    [{OPENCLAW_HOME:join(home,'custom-home'),OPENCLAW_STATE_DIR:'~/state'},join(home,'state','openclaw.json')],
    [{OPENCLAW_HOME:join(home,'custom-home'),OPENCLAW_STATE_DIR:'~/state',OPENCLAW_CONFIG_PATH:'~/config/custom.json'},join(home,'config','custom.json')],
  ]) {
    const resolved=await resolveConfig({agent:'openclaw',home,env});
    assert.equal(nativePath({config:resolved,scope:'user'},env),expected);
    assert.equal(resolved.paths.user,join(require('node:path').dirname(expected),'futureagi.json'));
  }
});
