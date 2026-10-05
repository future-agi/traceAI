// TEST CONTROL ONLY. The recipe never passes captureContent: true. This
// script does, so the contract test can prove that its "no content" check
// would catch prompt or completion text if it reached the collector.
import { otelMiddleware } from "@tanstack/ai/middlewares/otel";
import { answer } from "../src/chat.mjs";
import {
  flushTraces,
  registerFutureAgiTracing,
  shutdownTraces,
} from "../src/tracing.mjs";

const tracerProvider = registerFutureAgiTracing();
try {
  const middleware = otelMiddleware({
    tracer: tracerProvider.getTracer("capture-content-control"),
    captureContent: true,
  });
  console.log(await answer(process.argv[2], middleware));
} finally {
  await flushTraces(tracerProvider);
  await shutdownTraces(tracerProvider);
}
