// Child process for ambientOtlpHeaders.test.ts: one span through the auto init() path to FI_TEST_BASE_URL.
import * as auto from "../../src";
(async () => {
  const provider = auto.init({ apiKey: "synthetic-fi-key", secretKey: "synthetic-fi-secret", baseUrl: process.env.FI_TEST_BASE_URL, projectName: "ambient-headers",
    configFile: "/does-not-exist/ambient-headers-config", instrumentations: [], forwardApplicationSpans: true } as any);
  if (!provider) throw new Error("init returned no provider");
  provider.getTracer("ambient-headers").startSpan("one").end();
  await auto.forceFlush(); await auto.shutdown();
})().catch(e => { console.error(String(e)); process.exitCode = 1; });
