// Child process for fiRoute.test.ts: init() with the base URL in FI_TEST_BASE_URL and a complete key pair.
import * as auto from "../../src";
(async () => {
  const provider = auto.init({ apiKey: "synthetic-fi-key-0001", secretKey: "synthetic-fi-secret-0001", baseUrl: process.env.FI_TEST_BASE_URL, projectName: "refused",
    configFile: "/does-not-exist/refused-config", instrumentations: [], wireCapture: false } as any);
  console.log(provider ? "INIT_OK" : "INIT_NONE");
  await auto.shutdown();
})().catch(e => { console.error(String(e)); process.exitCode = 1; });
