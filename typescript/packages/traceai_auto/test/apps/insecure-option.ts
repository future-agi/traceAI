// Child process for allowInsecureHttp.test.ts: init() with a plaintext self-hosted base URL and allowInsecureHttp: true in code.
import * as auto from "../../src";
(async () => {
  const provider = auto.init({ apiKey: "synthetic-fi-key-0001", secretKey: "synthetic-fi-secret-0001", baseUrl: process.env.FI_TEST_BASE_URL, projectName: "insecure-option",
    allowInsecureHttp: process.env.FI_TEST_ALLOW === "1", configFile: "/does-not-exist/insecure-config", instrumentations: [], wireCapture: false } as any);
  console.log(provider ? "INIT_OK" : "INIT_NONE");
  await auto.shutdown();
})().catch(e => { console.error(String(e)); process.exitCode = 1; });
