const path = require('node:path');
const { runCLI } = require('jest');

// Test-scoped workaround, NOT a clean-lifecycle assertion. @voltagent/core 2.11.0
// leaves referenced 500 ms polling intervals after completed resumed workflows,
// even after full public app.shutdown(). Upstream-only reproductions do the same.
// Run only the resume regression in this process; ordinary tests run first in
// their own invocation, with open-handle detection and no forced exit.
// Await all assertions AND teardown, propagate Jest's real status, then exit to
// bound that known upstream leak. Never use Jest's suite-wide --forceExit.
(async () => {
  const root = path.resolve(__dirname, '..');
  const { results } = await runCLI({
    config: path.join(root, 'jest.resume.config.cjs'),
    runInBand: true,
    watch: false,
  }, [root]);
  const isolated = results.testResults.length === 1 && results.numTotalTests === 2;
  const code = results.success && isolated ? 0 : 1;
  const message = '\nIsolated resume suite finished; exiting for the documented VoltAgent 2.11.0 upstream interval leak.\n';
  process.stderr.write(message, () => process.exit(code));
})().catch(() => {
  process.stderr.write('Isolated resume runner failed.\n', () => process.exit(1));
});
