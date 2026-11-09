// Copied into a temporary application with a real `ai` package. Application
// calls have no telemetry options, manual instrumentors, or registerSecret calls.
const assert = require("node:assert/strict");
const sdk = require("ai");
const mocks = require("ai/test");
const { context, trace, SpanStatusCode, createTraceState, provider, raw, live, manager, core, auto, makeSecret } = globalThis.__vercelRegistryTest;
const [majorText, method, mode] = process.argv.slice(2);
const major = Number(majorText), traced = mode === "traced";

(async () => {
  const application = [];
  for (const intermediate of [false, true]) {
    // Not an environment credential or a marked field: only the transport may
    // discover this value. Distinct values prevent prior calls masking failures.
    const secret = makeSecret(intermediate);
    assert(!core.getRegisteredSecrets().includes(secret));
    const tracer = provider.getTracer("application " + secret, secret);
    const parent = tracer.startSpan("enclosing application", { links: [{
      context: { traceId: "3".repeat(32), spanId: "4".repeat(16), traceFlags: 1, traceState: createTraceState("vendor=" + secret) },
      attributes: { echoed: secret },
    }] });
    const parentContext = trace.setSpan(context.active(), parent);
    let providerCalls = 0, callbackCalls = 0, originalError;
    const transport = async () => {
      providerCalls++;
      const send = async () => {
        const url = process.env.MOCK_URL + "/v1/chat/completions";
        const requestBodyValues = { model: "fixture", messages: [{ role: "user", content: "ordinary visible input" }], stream: method === "streamText" };
        const response = await fetch(url, { method: "POST", headers: {
          "content-type": "application/json", authorization: "Bearer " + secret,
        }, body: JSON.stringify(requestBodyValues) });
        assert.equal(response.status, 401);
        const responseBody = await response.text();
        originalError = new sdk.APICallError({ message: JSON.parse(responseBody).error.message,
          url, requestBodyValues, statusCode: response.status, responseBody,
          responseHeaders: Object.fromEntries(response.headers), isRetryable: false });
        throw originalError;
      };
      if (!intermediate) return send();
      return tracer.startActiveSpan("sdk-native client", async span => {
        try { return await send(); } finally { span.end(); }
      });
    };
    const Model = mocks[`MockLanguageModelV${major === 5 ? 2 : major === 6 ? 3 : 4}`];
    const model = new Model({ provider: "loopback.test", modelId: "fixture", doGenerate: transport, doStream: transport });
    const observed = [];
    await context.with(parentContext, async () => {
      const options = { model, prompt: "ordinary visible input", maxRetries: 0,
        onError(event) { callbackCalls++; observed.push(event.error); } };
      if (method === "generateText") {
        try { await sdk.generateText(options); assert.fail("expected provider 401"); }
        catch (error) { observed.push(error); }
      } else {
        const result = sdk.streamText(options);
        assert(!(result instanceof Promise));
        try { for await (const part of result.fullStream) if (part.type === "error") observed.push(part.error); }
        catch (error) { observed.push(error); }
      }
      assert.equal(providerCalls, 1);
      assert(observed.length > 0);
      for (const error of observed) assert.equal(error, originalError);
      assert(originalError.message.includes(secret));
      assert.equal(core.getRegisteredSecrets().includes(secret), traced);
      parent.setAttributes({ echoed: [originalError.message, secret], safe: "ordinary visible input" });
      parent.setStatus({ code: SpanStatusCode.ERROR, message: originalError.message });
      parent.recordException(originalError);
      parent.addEvent("echo " + secret, { echoed: secret });
    });
    parent.end();
    // A fresh trace has no credential inheritance from the covered model span.
    const unrelated = provider.getTracer("unrelated").startSpan("later unrelated application", {
      attributes: { echoed: secret, safe: "ordinary visible input" },
    });
    unrelated.setStatus({ code: SpanStatusCode.ERROR, message: secret });
    unrelated.end();
    assert.notEqual(unrelated.spanContext().traceId, parent.spanContext().traceId);
    assert.equal(live.size, 0);
    // The agent's exporter must scrub snapshots, not the application's exporter.
    assert.equal(raw.find(span => span === parent).attributes.echoed[1], secret);
    assert.equal(parent.resource.attributes.echoed, makeSecret(false));
    assert.equal(parent.links[0].context.traceState.get("vendor"), secret);
    assert.equal(parent.instrumentationLibrary.version, secret);
    assert(parent.events.some(event => event.attributes?.["exception.message"]?.includes(secret)));
    application.push({ intermediate, providerCalls, callbackCalls, observed: observed.length,
      name: originalError.name, status: originalError.statusCode, message: originalError.message,
      responseBody: originalError.responseBody, secret });
  }
  if (traced) {
    assert(auto.report().active);
    assert.equal(auto.report().integrations.find(item => item.name === "vercel").status, "instrumented");
    await auto.forceFlush();
  }
  console.log(JSON.stringify({ application, raw: raw.map(span => ({
    id: span.spanContext().spanId, traceId: span.spanContext().traceId, name: span.name,
    kind: span.attributes["gen_ai.span.kind"], layer: span.attributes["traceai.capture.layer"], status: span.status.code,
  })), live: live.size }));
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (traced) await auto.shutdown();
  await provider.shutdown();
  context.disable(); trace.disable(); manager.disable();
});
