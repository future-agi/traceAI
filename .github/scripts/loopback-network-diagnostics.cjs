// Node's test reporter omits fetch's nested cause. Observe transport failures
// without wrapping fetch, retrying requests, or changing test outcomes.
const { channel } = require('node:diagnostics_channel');
const loopback = new Set(['localhost', '127.0.0.1', '[::1]', '::1']);

function report(event, hostname, port, error) {
  if (!loopback.has(hostname)) return;
  const causes = [];
  const seen = new Set();
  while (error && !seen.has(error) && causes.length < 5) {
    seen.add(error);
    causes.push({
      name: error.name,
      code: error.code,
      errno: error.errno,
      syscall: error.syscall,
      address: error.address,
      port: error.port,
    });
    error = error.cause;
  }
  console.error('LOOPBACK_TRANSPORT_ERROR', JSON.stringify({
    event, node: process.version, platform: process.platform, hostname, port, causes,
  }));
}

channel('undici:client:connectError').subscribe(({ connectParams, error }) => {
  report('connect', connectParams.hostname, connectParams.port, error);
});
channel('undici:request:error').subscribe(({ request, error }) => {
  const { hostname, port } = new URL(request.origin);
  report('request', hostname, port, error);
});
