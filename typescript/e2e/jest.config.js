module.exports = {
  preset: 'ts-jest',
  testEnvironment: 'node',
  roots: ['<rootDir>'],
  testMatch: ['**/*.e2e.test.ts'],
  transform: {
    '^.+\\.ts$': 'ts-jest'
  },
  moduleNameMapper: {
    '^@traceai/fi-core$': '<rootDir>/../packages/fi-core/src',
    '^@traceai/fi-semantic-conventions$': '<rootDir>/../packages/fi-semantic-conventions/src',
    '^@traceai/chromadb$': '<rootDir>/../packages/traceai_chromadb/src',
    '^@traceai/pinecone$': '<rootDir>/../packages/traceai_pinecone/src',
    '^@traceai/qdrant$': '<rootDir>/../packages/traceai_qdrant/src'
  },
  testTimeout: 60000,
  setupFilesAfterEnv: ['<rootDir>/setup.ts'],
  // Run tests sequentially to avoid conflicts
  maxWorkers: 1
};
