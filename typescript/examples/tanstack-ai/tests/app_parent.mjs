// Test fixture: an app with its own OpenTelemetry provider and context
// manager (as an HTTP server instrumentation or a framework's own tracing
// sets up) calls the route while its own span is active. Its spans are
// exported elsewhere, not to Future AGI. Pass "join" as the second argument
// to build the middleware with joinActiveTrace: true. Prints the app span's
// ids.
//
// @opentelemetry/sdk-trace-node is @traceai/fi-core's own dependency,
// pinned by package.json "overrides"; only this fixture imports it.
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { answer } from "../src/chat.mjs";
import {
  flushTraces,
  futureAgiOtelMiddleware,
  registerFutureAgiTracing,
  shutdownTraces,
} from "../src/tracing.mjs";

const [question, mode] = process.argv.slice(2);
const appProvider = new NodeTracerProvider();
appProvider.register(); // the app's global provider and AsyncLocalStorage context manager
const fiProvider = registerFutureAgiTracing();
try {
  await appProvider.getTracer("app").startActiveSpan("GET /chat", async (span) => {
    try {
      const middleware = futureAgiOtelMiddleware(fiProvider.getTracer("tanstack-ai"), {
        joinActiveTrace: mode === "join",
      });
      console.log(await answer(question, middleware));
      const { traceId, spanId } = span.spanContext();
      console.log(`appTraceId=${traceId} appSpanId=${spanId}`);
    } finally {
      span.end();
    }
  });
} finally {
  await flushTraces(fiProvider);
  await shutdownTraces(fiProvider);
  await appProvider.shutdown();
}
