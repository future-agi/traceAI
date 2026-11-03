// Local Runtime API contract harness, not a claim of an AWS deployment.
const { pathToFileURL } = require('node:url');
const [handlerFile, runtime] = process.argv.slice(2);
(async () => {
  const { handler } = await import(pathToFileURL(handlerFile).href);
  for (;;) {
    const next = await fetch(`${runtime}/2018-06-01/runtime/invocation/next`);
    if (next.status === 410) return;
    const requestId = next.headers.get('lambda-runtime-aws-request-id');
    const deadline = Number(next.headers.get('lambda-runtime-deadline-ms'));
    const invocation = { awsRequestId: requestId, functionName: 'orders', getRemainingTimeInMillis: () => deadline - Date.now() };
    let payload, suffix;
    try { payload = await handler(await next.json(), invocation); suffix = 'response'; }
    catch { payload = { errorType: 'ApplicationError' }; suffix = 'error'; }
    await fetch(`${runtime}/2018-06-01/runtime/invocation/${requestId}/${suffix}`, {
      method: 'POST', body: JSON.stringify(payload),
    });
  }
})().then(() => process.exit(0), error => { console.error(error); process.exit(1); });
