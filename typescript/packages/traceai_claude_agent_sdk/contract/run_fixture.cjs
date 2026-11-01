'use strict';
/**
 * Contract script for the shared Python harness (python/tests/harness).
 *
 * Runs the built package (dist/src, CommonJS) with the real fi-core register()
 * and the real OTLP/HTTP exporter. The Claude Agent SDK is replaced by a fake
 * query() that yields SDK-typed fixture messages: no CLI, no Anthropic call.
 *
 * Env:
 *   FI_BASE_URL, FI_API_KEY, FI_SECRET_KEY, FI_PROJECT_NAME  read by fi-core
 *   CONTRACT_FIXTURES_DIR  compiled src/__tests__/fixtures (messages.js, fakeQuery.js)
 *   JOURNEY                simple | subagent | mcp
 *
 * Prints {"yielded": [...], "expected": [...]} on stdout.
 */
const path = require('path');

const pkgDir = path.resolve(__dirname, '..');
const { register, ProjectType } = require(require.resolve('@traceai/fi-core', { paths: [pkgDir] }));
const { wrapQuery, shutdown } = require(pkgDir);
const fixtures = require(path.join(process.env.CONTRACT_FIXTURES_DIR, 'messages.js'));
const { makeFakeQuery } = require(path.join(process.env.CONTRACT_FIXTURES_DIR, 'fakeQuery.js'));

const JOURNEYS = {
  simple: fixtures.simpleToolJourney,
  subagent: fixtures.subagentJourney,
  mcp: fixtures.mcpErrorJourney,
};

async function main() {
  const journey = JOURNEYS[process.env.JOURNEY || 'simple'];
  if (!journey) throw new Error(`unknown JOURNEY ${process.env.JOURNEY}`);

  // Project name and collector endpoint come from FI_PROJECT_NAME / FI_BASE_URL.
  const provider = register({ projectType: ProjectType.OBSERVE, batch: true });

  const messages = journey();
  const expected = JSON.parse(JSON.stringify(messages));
  const fake = makeFakeQuery(messages);
  const query = wrapQuery(fake.query, { tracerProvider: provider });

  const yielded = [];
  for await (const message of query({ prompt: fixtures.PROMPT, options: { model: fixtures.MODEL } })) {
    yielded.push(message);
  }

  await shutdown();
  await provider.shutdown();
  process.stdout.write(JSON.stringify({ yielded, expected }));
}

main().catch((error) => {
  process.stderr.write(`${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
});
