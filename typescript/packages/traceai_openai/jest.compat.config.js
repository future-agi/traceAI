const path = require('node:path');
const config = require('./jest.config');

if (!process.env.OPENAI_TEST_MODULE) {
  throw new Error('Set OPENAI_TEST_MODULE to an isolated openai installation');
}
const sdk = path.resolve(process.env.OPENAI_TEST_MODULE);
module.exports = {
  ...config,
  testMatch: ['**/providerAttribution.test.ts', '**/providerAttributes.test.ts'],
  moduleNameMapper: {
    ...config.moduleNameMapper,
    '^openai$': sdk,
    '^openai/(.*)$': `${sdk}/$1`,
  },
};
