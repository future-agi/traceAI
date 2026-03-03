module.exports = {
  preset: 'ts-jest',
  testEnvironment: 'node',
  roots: ['<rootDir>/src'],
  testMatch: [
    '**/__tests__/**/*.test.ts',
    '**/?(*.)+(spec|test).ts'
  ],
  testPathIgnorePatterns: ['/node_modules/', 'e2e\\.test\\.ts$'],
  collectCoverageFrom: [
    'src/**/*.ts',
    '!src/**/*.d.ts',
    '!src/index.ts'
  ],
  setupFilesAfterEnv: ['<rootDir>/../../jest.setup.js'],
  coverageDirectory: 'coverage',
  coverageReporters: ['text', 'lcov'],
  transformIgnorePatterns: ['node_modules/(?!\\.pnpm|@llamaindex)'],
  transform: {
    '^.+\\.[jt]s$': ['ts-jest', {
      tsconfig: {
        module: 'commonjs'
      },
      diagnostics: false,
    }]
  },
  moduleNameMapper: {
    '^@traceai/fi-core$': '<rootDir>/../fi-core/src',
    '^@traceai/fi-semantic-conventions$': '<rootDir>/../fi-semantic-conventions/src'
  },
  testTimeout: 10000
};
