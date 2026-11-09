// Copy test/ into the clean tarball-install project; run with FIXTURES and MODE=cjs|esm.
const assert = require('node:assert/strict');
const path = require('node:path');
const { context, trace } = require('@opentelemetry/api');
const { suppressTracing } = require('@opentelemetry/core');
const telemetry = require('./telemetry.cjs');

(async () => {
  assert.ok(process.env.FIXTURES, 'stage fixtures before running contracts');
  const mode = process.env.MODE || 'cjs';
  const sdk = mode === 'esm' ? await import('@huggingface/transformers') : require('@huggingface/transformers');
  const adapter = mode === 'esm' ? await import('@traceai/huggingface-transformers') : require('@traceai/huggingface-transformers');
  const t = await telemetry.start();
  const instrumentor = new adapter.TransformersInstrumentation();
  const facade = instrumentor.wrapModule(sdk);
  assert.notEqual(facade, sdk);
  assert.equal(instrumentor.wrapModule(facade), facade);
  assert.equal(facade.Tensor, sdk.Tensor);
  sdk.env.allowRemoteModels = false;
  sdk.env.localModelPath = path.resolve(process.env.FIXTURES) + path.sep;
  const load = { dtype: 'fp32', device: 'cpu', local_files_only: true, session_options: { intraOpNumThreads: 1, interOpNumThreads: 1 } };
  const generation = { max_new_tokens: 3, do_sample: false };
  const tasks = [
    ['text-generation', 'llama', [['Hello', generation], [['Hello', 'Goodbye'], generation], [[{ role: 'user', content: 'Hello' }], generation], [[[{ role: 'user', content: 'Hello' }], [{ role: 'user', content: 'Goodbye' }]], generation]]],
    ['text2text-generation', 't5', [['Hello', generation], [['Hello', 'Goodbye'], generation]]],
    ['summarization', 't5', [['Alice lives in Paris.', generation], [['Alice lives in Paris.', 'Bob likes coffee.'], generation]]],
    ['feature-extraction', 'bert', [['Hello', { pooling: 'mean', normalize: true }], [['Hello', 'Goodbye'], { pooling: 'mean' }]]],
    ['question-answering', 'qa', [['Who lives in Paris?', 'Alice lives in Paris.'], [['Who?', 'Where?'], ['Alice lives in Paris.', 'Bob lives in London.']]]],
  ];
  let invocationCount = 0;
  const loaded = [];
  try {
    for (const [task, model, cases] of tasks) {
      const beforeLoad = t.memory.getFinishedSpans().length;
      const raw = await sdk.pipeline(task, model, load);
      loaded.push(raw);
      assert.equal(t.memory.getFinishedSpans().length, beforeLoad);
      let rawReturn;
      // Observe the exact object produced by a real pipeline, without substituting its execution.
      const observed = new Proxy(raw, { apply: async (target, thisArg, args) => (rawReturn = await Reflect.apply(target, thisArg, args)) });
      const wrapped = instrumentor.wrapPipeline(observed);
      assert.equal(instrumentor.wrapPipeline(wrapped), wrapped);
      assert.equal(wrapped.task, raw.task); assert.equal(wrapped.model, raw.model); assert.equal(wrapped.tokenizer, raw.tokenizer);
      for (const args of cases) {
        const expected = await raw(...args);
        const before = t.memory.getFinishedSpans().length;
        const actual = await wrapped(...args);
        invocationCount++;
        assert.equal(actual, rawReturn, 'do not replace Tensor/result identity');
        assert.deepEqual(actual, expected, `${task}: traced/untraced behavior`);
        assert.equal(t.memory.getFinishedSpans().length, before + 1);
        const span = t.memory.getFinishedSpans().at(-1);
        assert.equal(span.name, `transformers.${task}`); assert.equal(span.kind, 0);
        assert.equal(span.attributes['fi.span.kind'], task === 'feature-extraction' ? 'EMBEDDING' : task === 'question-answering' ? 'CHAIN' : 'LLM');
        assert.equal(span.attributes['transformers.task'], task);
        assert.equal(span.attributes['gen_ai.provider.name'], 'huggingface');
        assert.ok(Object.keys(span.attributes).some(k => k.endsWith('model') || k.endsWith('model_name')), 'actual model identity');
        if (task === 'feature-extraction') {
          assert.ok(actual instanceof sdk.Tensor);
          assert.deepEqual(span.attributes['transformers.output.shape'], actual.dims);
          assert.equal(span.attributes['transformers.embedding.dimension'], actual.dims.at(-1));
          assert.ok(!('output.value' in span.attributes));
        } else if (task !== 'question-answering') {
          assert.equal(JSON.parse(span.attributes['gen_ai.request.parameters']).max_new_tokens, 3);
        } else {
          const input = JSON.parse(span.attributes['input.value']);
          assert.deepEqual(input, { question: args[0], context: args[1] });
        }
        for (const key of Object.keys(span.attributes)) assert.ok(!/usage|cost|finish_reason|first_token/.test(key));
      }
      const count = t.memory.getFinishedSpans().length;
      instrumentor.disable(); await wrapped(...cases[0]); instrumentor.enable();
      await context.with(suppressTracing(context.active()), () => wrapped(...cases[0]));
      assert.equal(t.memory.getFinishedSpans().length, count);

      // Every content privacy flag is exercised with a real pipeline, and decoded at the wire.
      for (const flag of ['hideInputs', 'hideOutputs', 'hideInputText', 'hideInputMessages', 'hideOutputText', 'hideOutputMessages', 'hideEmbeddingVectors']) {
        const privatePipeline = new adapter.TransformersInstrumentation({ traceConfig: { [flag]: true } }).wrapPipeline(raw);
        await t.flush(); const wireBefore = t.wire().length;
        await privatePipeline(...cases[0]); invocationCount++;
        await t.flush();
        const [span] = t.wire().slice(wireBefore);
        assert.ok(span);
        const attrs = telemetry.attributes(span);
        if (flag.startsWith('hideInput')) assert.ok(!('input.value' in attrs));
        if (flag.startsWith('hideInput') || flag.startsWith('hideOutput') || task === 'feature-extraction') assert.ok(!('output.value' in attrs));
        assert.ok(!Object.keys(attrs).some(k => k.includes('vector')));
      }
      if (task === 'text-generation') {
        // Standard streamer callbacks are untouched; span stays active through their last delivery.
        const makeStreamer = (events, traced) => new sdk.TextStreamer(raw.tokenizer, {
          skip_prompt: true,
          callback_function: text => { events.push(['text', text]); if (traced) assert.equal(trace.getActiveSpan()?.isRecording(), true); },
          token_callback_function: tokens => { events.push(['tokens', tokens]); if (traced) assert.equal(trace.getActiveSpan()?.isRecording(), true); },
        });
        const plainEvents = [], tracedEvents = [];
        const expected = await raw('Hello', { ...generation, streamer: makeStreamer(plainEvents, false) });
        const streamer = makeStreamer(tracedEvents, true);
        const options = { ...generation, streamer };
        const actual = await wrapped('Hello', options); invocationCount++;
        assert.equal(options.streamer, streamer); assert.deepEqual(actual, expected); assert.deepEqual(tracedEvents, plainEvents); assert.ok(tracedEvents.length);
        const stopping = new sdk.InterruptableStoppingCriteria(); stopping.interrupt();
        const stoppedRaw = await raw('Hello', { ...generation, stopping_criteria: [stopping] });
        const stoppedTraced = await wrapped('Hello', { ...generation, stopping_criteria: [stopping] }); invocationCount++;
        assert.deepEqual(stoppedTraced, stoppedRaw);
        // piiRedaction alone must mask messages AND stacks without hiding all error details.
        for (const source of ['option', 'environment']) {
          const previous = process.env.FI_PII_REDACTION;
          let privatePipeline;
          try {
            if (source === 'environment') process.env.FI_PII_REDACTION = 'true';
            privatePipeline = new adapter.TransformersInstrumentation(source === 'option' ? { traceConfig: { piiRedaction: true } } : {}).wrapPipeline(raw);
          } finally {
            if (previous === undefined) delete process.env.FI_PII_REDACTION;
            else process.env.FI_PII_REDACTION = previous;
          }
          const marker = 'error.person@example.com';
          const error = new Error(`Callback failed for ${marker}`);
          const originalStack = error.stack;
          await t.flush(); const wireBefore = t.wire().length;
          await assert.rejects(privatePipeline('Hello', { ...generation, streamer: new sdk.TextStreamer(raw.tokenizer, {
            skip_prompt: true, callback_function: () => { throw error; },
          }) }), thrown => thrown === error); invocationCount++;
          assert.equal(error.message, `Callback failed for ${marker}`);
          assert.equal(error.stack, originalStack);
          await t.flush();
          const spans = t.wire().slice(wireBefore);
          assert.equal(spans.length, 1);
          assert.equal(spans[0].status.code, 2);
          const [event] = spans[0].events;
          assert.equal(event.name, 'exception');
          const attrs = telemetry.attributes(event);
          assert.equal(attrs['exception.type'], 'Error');
          assert.equal(attrs['exception.message'], 'Callback failed for <EMAIL_ADDRESS>');
          assert.ok(attrs['exception.stacktrace'].includes('Callback failed for <EMAIL_ADDRESS>'));
          assert.ok(!JSON.stringify(spans).includes(marker), `${source}: PII must not reach OTLP`);
        }
        const callbackError = new Error('PRIVATE_CALLBACK_ERROR');
        const privateError = new adapter.TransformersInstrumentation({ traceConfig: { hideErrorDetails: true } }).wrapPipeline(raw);
        await t.flush(); const errorBefore = t.wire().length;
        const before = t.memory.getFinishedSpans().length;
        await assert.rejects(privateError('Hello', { ...generation, streamer: new sdk.TextStreamer(raw.tokenizer, {
          skip_prompt: true, callback_function: () => { throw callbackError; },
        }) }), error => error === callbackError); invocationCount++;
        assert.equal(t.memory.getFinishedSpans().length, before + 1);
        assert.equal(t.memory.getFinishedSpans().at(-1).status.code, 2);
        await t.flush(); assert.ok(!JSON.stringify(t.wire().slice(errorBefore)).includes('PRIVATE_CALLBACK_ERROR'));

        const parents = [];
        await Promise.all(['one', 'two'].map(label => t.provider.getTracer('contracts').startActiveSpan(label, async parent => {
          parents.push(parent.spanContext()); await wrapped(label, generation); invocationCount++; parent.end();
        })));
        await t.flush();
        for (const parent of parents) {
          const children = t.wire().filter(s => s.parentSpanId === parent.spanId);
          assert.equal(children.length, 1); assert.equal(children[0].traceId, parent.traceId);
        }
      }
      console.log('PASS', mode, task, cases.length, 'behavior cases + seven privacy flags');
    }
    const beforeFactory = t.memory.getFinishedSpans().length;
    const factoryPipeline = await facade.pipeline('feature-extraction', 'bert', load);
    loaded.push(factoryPipeline);
    assert.equal(t.memory.getFinishedSpans().length, beforeFactory);
    await factoryPipeline('module facade'); invocationCount++;
    const beforeDispose = t.memory.getFinishedSpans().length;
    for (const p of loaded.splice(0)) await p.dispose();
    assert.equal(t.memory.getFinishedSpans().length, beforeDispose);
    await t.flush();
    const invocations = t.wire().filter(s => s.name.startsWith('transformers.'));
    assert.equal(invocations.length, invocationCount);
    for (const span of invocations) {
      assert.equal(telemetry.attributes(span)['gen_ai.provider.name'], 'huggingface', span.name);
      assert.equal(span.kind, 1, 'local execution remains OTLP INTERNAL');
    }
    console.log(JSON.stringify({ mode, node: process.version, invocations: invocationCount, otlpSpans: t.wire().length, status: 'PASS' }));
  } finally {
    for (const p of loaded) await p.dispose();
    await t.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
