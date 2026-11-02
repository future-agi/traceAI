module.exports = {
  preset: "ts-jest",
  testEnvironment: "node",
  testMatch: ["**/src/**/*.test.ts"],
  testPathIgnorePatterns: ["/node_modules/", "e2e\\.test\\.ts$"],
  moduleFileExtensions: ["ts", "js", "json"],
  // Test against workspace sources so the shared route does not need a prior build.
  moduleNameMapper: {
    "^@traceai/openai$": "<rootDir>/../traceai_openai/src",
    "^@traceai/fi-core$": "<rootDir>/../fi-core/src",
    "^@traceai/fi-semantic-conventions$": "<rootDir>/../fi-semantic-conventions/src",
  },
  collectCoverageFrom: ["src/**/*.ts", "!src/**/*.test.ts", "!src/**/__tests__/**"],
  coverageDirectory: "coverage",
  verbose: true,
};
