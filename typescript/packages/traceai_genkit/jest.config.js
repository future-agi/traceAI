module.exports = {
  preset: 'ts-jest',
  testEnvironment: 'node',
  cacheDirectory: '<rootDir>/node_modules/.cache/jest',
  roots: ['<rootDir>/src'],
  testMatch: ['**/__tests__/**/*.test.ts'],
  testPathIgnorePatterns: ['/node_modules/'],
  collectCoverageFrom: ['src/**/*.ts', '!src/**/*.d.ts', '!src/index.ts', '!src/__tests__/**'],
  coverageDirectory: 'coverage',
  coverageReporters: ['text', 'lcov'],
  transform: {
    '^.+\\.ts$': ['ts-jest', {
      tsconfig: {
        module: 'commonjs',
        target: 'es2020',
        lib: ['esnext', 'dom', 'dom.iterable'],
        strict: true,
        esModuleInterop: true,
        resolveJsonModule: true,
        skipLibCheck: true
      }
    }]
  },
  moduleNameMapper: {
    '^@traceai/fi-core$': '<rootDir>/../fi-core/src',
    '^@traceai/fi-semantic-conventions$': '<rootDir>/../fi-semantic-conventions/src'
  },
  testTimeout: 15000
};
