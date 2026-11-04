const base = require('./jest.config.js');

module.exports = {
  ...base,
  testPathIgnorePatterns: [],
  testMatch: ['<rootDir>/src/__tests__/workflow-resume.test.ts'],
};
