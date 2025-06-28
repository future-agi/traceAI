module.exports = {
  preset: 'ts-jest',
  testEnvironment: 'node',
  roots: ['<rootDir>/packages'],
  testMatch: [
    '**/packages/**/__tests__/**/*.test.ts',
    '**/packages/**/?(*.)+(spec|test).ts'
  ],
  collectCoverageFrom: [
    '**/packages/**/src/**/*.ts',
    '!**/packages/**/src/**/*.d.ts',
    '!**/packages/**/src/**/index.ts',
    '!**/packages/**/src/generated/**',
    '!**/node_modules/**',
    '!**/dist/**'
  ],
  coverageDirectory: 'coverage',
  coverageReporters: ['text', 'lcov', 'html'],
  setupFilesAfterEnv: ['<rootDir>/jest.setup.js'],
  moduleNameMapper: {
    '^@traceai/(.*)$': '<rootDir>/packages/$1/src'
  },
  transform: {
    '^.+\\.ts$': 'ts-jest'
  },
  testTimeout: 10000
}; 