// Child process for sendGuardReal.test.ts: init() against the base URL named in FI_TEST_BASE_URL, one span, flush, shutdown.
import * as auto from "../../src";
(async () => {
  const provider = auto.init({ apiKey: "synthetic-fi-key-0001", secretKey: "synthetic-fi-secret-0001", baseUrl: process.env.FI_TEST_BASE_URL, projectName: "real-send",
    allowInsecureHttp: process.env.FI_TEST_ALLOW === "1", configFile: "/does-not-exist/real-send-config", instrumentations: [], wireCapture: false, forwardApplicationSpans: true } as any);
  console.log(provider ? "INIT_OK" : "INIT_NONE");
  if (provider) { provider.getTracer("real-send").startSpan("one").end(); await auto.forceFlush(); }
  await auto.shutdown();
})().catch(e => { console.error(String(e)); process.exitCode = 1; });
