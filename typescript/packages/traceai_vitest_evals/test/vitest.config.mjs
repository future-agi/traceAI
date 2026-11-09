import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { VitestEvalsReporter } = require('../dist/index.js');
export default {
  test: { include: ['test/eval.fixture.test.mjs'], reporters: ['default', new VitestEvalsReporter({
    endpoint: process.env.TEST_OTLP_ENDPOINT,
    apiKey: 'synthetic-key', secretKey: 'synthetic-secret',
  })],
  },
};
