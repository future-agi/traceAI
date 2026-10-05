// Test fixture: serve one HTTP request with a route over node:http, and abort
// the request's signal when the client disconnects before the answer is
// sent, as a server does with a Fetch API request.signal or res.on("close").
// Prints the port once listening, then how the route ended.
//
// Run directly, it serves src/chat.mjs's chatRoute. The contract test also
// imports serveOnce() from the README route.
import { createServer } from "node:http";
import { pathToFileURL } from "node:url";

/** Serve one request with `route(signal)`, then close the server. */
export function serveOnce(route) {
  return new Promise((resolve) => {
    const server = createServer((request, response) => {
      const disconnect = new AbortController();
      response.on("close", () => {
        if (!response.writableFinished) disconnect.abort();
      });
      route(disconnect.signal)
        .then(
          (text) => {
            console.log(`outcome=returned disconnected=${disconnect.signal.aborted}`);
            response.end(text);
          },
          (error) => {
            console.log(`outcome=threw ${error?.name ?? "error"}`);
            response.end();
          },
        )
        .finally(() => server.close(() => resolve()));
    });
    server.listen(0, "127.0.0.1", () => {
      console.log(`port=${server.address().port}`);
    });
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const { chatRoute } = await import("../src/chat.mjs");
  const { registerFutureAgiTracing, shutdownTraces } = await import("../src/tracing.mjs");
  const tracerProvider = registerFutureAgiTracing();
  try {
    await serveOnce((signal) => chatRoute(process.argv[2], tracerProvider, { signal }));
  } finally {
    await shutdownTraces(tracerProvider);
  }
}
