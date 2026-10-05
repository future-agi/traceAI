module.exports = {
  preset: 'ts-jest',
  testEnvironment: 'node',
  roots: ['<rootDir>/src'],
  testMatch: [
    '**/__tests__/**/*.test.ts',
    '**/?(*.)+(spec|test).ts'
  ],
  collectCoverageFrom: [
    'src/**/*.ts',
    '!src/**/*.d.ts',
    '!src/index.ts'
  ],
  coverageDirectory: 'coverage',
  coverageReporters: ['text', 'lcov'],
  transform: {
    '^.+\\.ts$': 'ts-jest'
  },
  moduleNameMapper: {
    // Source uses NodeNext `.js` import specifiers; map them back to the .ts source.
    '^(\\.{1,2}/.*)\\.js$': '$1',
    '^@traceai/fi-core$': '<rootDir>/../fi-core/src',
    '^@traceai/fi-semantic-conventions$': '<rootDir>/../fi-semantic-conventions/src'
  },
  testTimeout: 10000
}; 