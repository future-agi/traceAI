module.exports = {
  preset: 'ts-jest',
  testEnvironment: 'node',
  roots: ['<rootDir>/src'],
  testMatch: ['**/__tests__/**/*.test.ts'],
  testPathIgnorePatterns: ['/node_modules/'],
  collectCoverageFrom: ['src/**/*.ts', '!src/**/*.d.ts', '!src/index.ts', '!src/__tests__/**'],
  coverageDirectory: 'coverage',
  coverageReporters: ['text', 'lcov'],
  transform: {
    '^.+\\.ts$': ['ts-jest', {
      tsconfig: {
        module: 'commonjs'
      }
    }]
  },
  moduleNameMapper: {
    '^@traceai/fi-core$': '<rootDir>/../fi-core/src',
    '^@traceai/fi-semantic-conventions$': '<rootDir>/../fi-semantic-conventions/src'
  },
  testTimeout: 15000
};
