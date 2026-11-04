// TEST CONTROL. The recipe leaves content capture off. This script turns on
// the documented opt-in, futureAgiOtelMiddleware(tracer, { captureContent:
// true }), so the contract test can prove two things: the opt-in works, and
// the "no content" checks would catch prompt or completion text if it
// reached the collector.
import { answer } from "../src/chat.mjs";
import {
  flushTraces,
  futureAgiOtelMiddleware,
  registerFutureAgiTracing,
  shutdownTraces,
} from "../src/tracing.mjs";

const tracerProvider = registerFutureAgiTracing();
try {
  const middleware = futureAgiOtelMiddleware(
    tracerProvider.getTracer("capture-content-control"),
    { captureContent: true },
  );
  console.log(await answer(process.argv[2], middleware));
} finally {
  await flushTraces(tracerProvider);
  await shutdownTraces(tracerProvider);
}
