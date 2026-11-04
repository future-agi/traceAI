"""Execute the workflow's credential gate with booleans only, without credentials or network."""
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / 'workflows/release.yml'


class CredentialGateTests(unittest.TestCase):
    def run_gate(self, *, token=False, trusted=False, release=True, pending=True):
        workflow = WORKFLOW.read_text()
        step = workflow.split('      - name: Credential and event gates\n', 1)[1].split('\n  python-core-build:', 1)[0]
        # Bind the exercised booleans to the real workflow inputs. Never expose a token.
        self.assertIn("NPM_TOKEN_AVAILABLE: ${{ secrets.NPM_TOKEN != '' }}", step)
        self.assertIn("NPM_TRUSTED_PUBLISHING: ${{ vars.NPM_TRUSTED_PUBLISHING == 'true' }}", step)
        self.assertIn("TYPESCRIPT_PENDING: ${{ steps.plan.outputs.typescript != '[]' }}", step)
        self.assertIn("RELEASE_EVENT: ${{ github.ref == 'refs/heads/main' && github.event_name != 'pull_request' && inputs.dry_run != true }}", step)
        script = textwrap.dedent(step.split('        run: |\n', 1)[1])
        with tempfile.TemporaryDirectory() as d:
            output, summary = Path(d) / 'output', Path(d) / 'summary'
            output.touch()
            env = {
                'PATH': os.defpath,
                'RELEASE_EVENT': str(release).lower(),
                'NPM_TOKEN_AVAILABLE': str(token).lower(),
                'NPM_TRUSTED_PUBLISHING': str(trusted).lower(),
                'TYPESCRIPT_PENDING': str(pending).lower(),
                'PYPI': 'true', 'MAVEN': 'true', 'NUGET': 'true',
                'GITHUB_OUTPUT': str(output), 'GITHUB_STEP_SUMMARY': str(summary),
            }
            result = subprocess.run(['bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', script],
                                    env=env, capture_output=True, text=True)
            outputs = dict(line.split('=', 1) for line in output.read_text().splitlines())
        return result, outputs

    def test_token_only_allows_npm_publishing(self):
        result, outputs = self.run_gate(token=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs['typescript'], 'true')
        self.assertNotIn('::warning::', result.stdout)

    def test_oidc_only_allows_npm_publishing_and_reconciliation(self):
        result, outputs = self.run_gate(trusted=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs['typescript'], 'true')
        self.assertNotIn('::warning::', result.stdout)

    def test_both_credentials_remain_supported(self):
        result, outputs = self.run_gate(token=True, trusted=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs['typescript'], 'true')

    def test_no_npm_configuration_fails_pending_release(self):
        result, outputs = self.run_gate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('::error::npm releases are pending', result.stdout)
        self.assertIn('NPM_TOKEN nor NPM_TRUSTED_PUBLISHING', result.stdout)
        self.assertEqual(outputs, {})

    def test_non_release_events_never_publish_or_require_credentials(self):
        for token, trusted in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(token=token, trusted=trusted):
                result, outputs = self.run_gate(token=token, trusted=trusted, release=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(all(value == 'false' for value in outputs.values()))
                self.assertNotIn('::error::', result.stdout)

    def test_non_npm_selection_does_not_require_npm_credentials(self):
        result, outputs = self.run_gate(pending=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs['typescript'], 'false')
        for eco in ('python', 'java', 'go', 'nuget'):
            self.assertEqual(outputs[eco], 'true')


if __name__ == '__main__':
    unittest.main()
