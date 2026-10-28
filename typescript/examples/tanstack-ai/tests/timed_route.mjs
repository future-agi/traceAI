// Test fixture: run the route once and print how long it took, so the
// contract test can check that a slow collector does not hold the response.
import { chatRoute } from "../src/chat.mjs";
import { registerFutureAgiTracing, shutdownTraces } from "../src/tracing.mjs";

const tracerProvider = registerFutureAgiTracing();
const started = performance.now();
try {
  const text = await chatRoute(process.argv[2], tracerProvider);
  console.log(`routeMs=${Math.round(performance.now() - started)}`);
  console.log(text);
} finally {
  await shutdownTraces(tracerProvider);
}
