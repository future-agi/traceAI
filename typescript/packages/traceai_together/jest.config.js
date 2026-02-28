/** @type {import('jest').Config} */
module.exports = {
  preset: "ts-jest",
  testEnvironment: "node",
  testMatch: ["**/src/**/*.test.ts"],
  testPathIgnorePatterns: ["/node_modules/", "e2e\\.test\\.ts$"],
  moduleFileExtensions: ["ts", "js", "json"],
  collectCoverageFrom: ["src/**/*.ts", "!src/**/*.test.ts", "!src/**/__tests__/**"],
  coverageDirectory: "coverage",
  verbose: true,
};
