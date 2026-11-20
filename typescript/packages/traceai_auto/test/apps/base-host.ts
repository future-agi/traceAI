// Child process for baseUrlHost.test.ts: init() twice with the configuration the test supplies; no span is exported.
import * as auto from "../../src";
(async () => {
  const options = { instrumentations: [], wireCapture: false } as any;
  if (process.env.FI_TEST_CONFIG_FILE) options.configFile = process.env.FI_TEST_CONFIG_FILE;
  if (process.env.FI_TEST_BASE_URL) options.baseUrl = process.env.FI_TEST_BASE_URL;
  auto.init(options); auto.init(options);
  await auto.shutdown();
})().catch(e => { console.error(String(e)); process.exitCode = 1; });
