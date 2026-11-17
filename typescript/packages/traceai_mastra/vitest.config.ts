import { defineConfig } from "vitest/config";

export default defineConfig({
  // Vite's default TypeScript filter excludes .cts modules.
  esbuild: { include: /\.(?:[cm]?ts|[jt]sx)$/ },
  test: {
    globals: true,
    environment: "node",
    include: ["src/__tests__/**/*.test.ts"],
    pool: "forks",
    poolOptions: { forks: { singleFork: true } },
    testTimeout: 10000,
  },
});
