"""Offline checks of the NuGet artifact handoff, OIDC boundary and registry verification."""
import io
import os
import re
import textwrap
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


WORKFLOWS = Path(__file__).resolve().parents[1] / 'workflows'


class NuGetWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (WORKFLOWS / 'release.yml').read_text()
        self.build = (WORKFLOWS / 'release-nuget.yml').read_text()
        self.caller = self.workflow.split('\n  nuget-build:\n', 1)[1].split('\n  nuget:\n', 1)[0]
        self.publish = self.workflow.split('\n  nuget:\n', 1)[1].split('\n  release:\n', 1)[0]
        self.verdict = self.workflow.split('\n  release:\n', 1)[1].split('\n  release-pr:\n', 1)[0]

    def test_build_is_read_only_and_receives_no_credentials(self):
        self.assertIn('permissions:\n  contents: read\n', self.build)
        self.assertIn('    permissions:\n      contents: read\n', self.caller)
        self.assertIn('needs: plan', self.caller)
        self.assertIn("if: github.event_name != 'pull_request' && needs.plan.outputs.nuget != '[]'", self.caller)
        self.assertIn('uses: ./.github/workflows/release-nuget.yml', self.caller)
        self.assertIn('packages: ${{ needs.plan.outputs.nuget }}', self.caller)
        self.assertIn('include: ${{ fromJson(inputs.packages) }}', self.build)
        for text in (self.build, self.caller):
            for forbidden in ('contents: write', 'id-token:', 'secrets:', 'secrets.', 'publish:',
                              'NuGet/login', 'dotnet nuget push', 'github_release.py', 'v3-flatcontainer'):
                self.assertNotIn(forbidden, text)
        self.assertLess(self.build.index('dotnet pack'), self.build.index('dotnet build smoke --no-restore'))
        self.assertLess(self.build.index('dotnet build smoke --no-restore'), self.build.index('actions/upload-artifact@'))

    def test_publish_is_top_level_gated_and_included_in_verdict(self):
        self.assertIn('needs: [plan, nuget-build]', self.publish)
        self.assertIn("!cancelled() && needs.nuget-build.result == 'success'\n      && needs.plan.outputs.publish_nuget == 'true'", self.publish)
        self.assertIn('environment: nuget', self.publish)
        self.assertIn('    permissions:\n      contents: write\n      id-token: write\n', self.publish)
        self.assertIn('include: ${{ fromJson(needs.plan.outputs.nuget) }}', self.publish)
        self.assertNotIn('uses: ./.github/workflows/', self.publish)
        self.assertNotIn('id-token:', self.workflow.split('\njobs:\n', 1)[0])
        needs = self.verdict.split('needs: [', 1)[1].split(']', 1)[0].split(', ')
        self.assertIn('nuget-build', needs)
        self.assertIn('nuget', needs)
        self.assertIn('if: always()', self.verdict)
        self.assertIn('RESULTS: ${{ toJson(needs) }}', self.verdict)
        self.assertIn('any(. == "failure" or . == "cancelled")', self.verdict)

    def test_artifact_handoff_and_actions_remain_pinned(self):
        for text in (self.build, self.publish):
            self.assertIn('name: ${{ matrix.tag }}', text)
            self.assertIn('persist-credentials: false', text)
            self.assertIn('actions/setup-dotnet@67a3573c9a986a3f9c594539f4ab511d57bb3ce9', text)
            self.assertIn("dotnet-version: '10.0.x'", text)
        self.assertIn('path: ${{ runner.temp }}/dist/*', self.build)
        self.assertIn('if-no-files-found: error', self.build)
        self.assertIn('path: ${{ runner.temp }}/dist\n', self.publish)
        self.assertIn('actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093', self.publish)
        for action in re.findall(r'\buses: (\S+)', self.workflow + self.build):
            if not action.startswith('./'):
                self.assertRegex(action, r'^[^@]+@[0-9a-f]{40}$')

    def test_temporary_login_is_immediately_before_push_and_skipped_on_reconcile(self):
        steps = self.publish.split('      - ')[1:]
        login_index = next(i for i, step in enumerate(steps) if 'NuGet/login@' in step)
        login, push = steps[login_index:login_index + 2]
        self.assertIn('NuGet/login@8d196754b4036150537f80ac539e15c2f1028841', login)
        self.assertIn('id: nuget-login', login)
        self.assertIn('user: ${{ vars.NUGET_USER }}', login)
        for step in (login, push):
            self.assertIn("if: '!matrix.registry_published'", step)
        self.assertIn('NUGET_PUSH_KEY: ${{ steps.nuget-login.outputs.NUGET_API_KEY }}', push)
        self.assertIn('dotnet nuget push "$RUNNER_TEMP/dist/*.nupkg" --api-key "$NUGET_PUSH_KEY" --source https://api.nuget.org/v3/index.json --skip-duplicate', push)
        self.assertNotIn('secrets.', self.publish)

    def test_reconcile_verifies_registry_before_creating_release(self):
        verify, release = self.publish.split('      - name: Verify the public registry version\n', 1)[1].split('      - name: Tag and create the GitHub release\n', 1)
        self.assertNotIn('        if:', verify + release)
        self.assertIn('v3-flatcontainer/', verify)
        self.assertIn('GH_TOKEN: ${{ github.token }}', release)
        self.assertIn('python3 .github/release/github_release.py --install "dotnet add package $NAME --version $VERSION" --dist "$RUNNER_TEMP/dist"', release)

    def test_matrix_values_are_passed_through_environment_not_shell_source(self):
        for key in ('name', 'version', 'tag', 'changelog', 'registry_published'):
            self.assertIn(f'{key.upper()}: ${{{{ matrix.{key} }}}}', self.publish)
        for key in ('name', 'version', 'project'):
            self.assertIn(f'{key.upper()}: ${{{{ matrix.{key} }}}}', self.build)
        for text in (self.publish, self.build):
            for step in text.split('      - '):
                if '        run:' in step:
                    self.assertNotIn('${{', step.split('        run:', 1)[1])

    def verify_registry(self):
        script = self.publish.split("          python3 - <<'PY'\n", 1)[1].split('\n          PY', 1)[0]
        with patch.dict(os.environ, {'NAME': 'FI-Instrumentation-Otel', 'VERSION': '1.0.1'}, clear=True):
            exec(compile(textwrap.dedent(script), 'nuget-registry-verification', 'exec'), {})

    def test_registry_verification_accepts_served_version(self):
        with patch('urllib.request.urlopen', return_value=io.StringIO('{"versions": ["1.0.1"]}')) as get, patch('time.sleep') as sleep:
            self.verify_registry()
        get.assert_called_once_with('https://api.nuget.org/v3-flatcontainer/fi-instrumentation-otel/index.json', timeout=30)
        sleep.assert_not_called()

    def test_registry_verification_retries_missing_package_and_version(self):
        replies = [urllib.error.HTTPError('url', 404, 'missing', {}, None),
                   io.StringIO('{"versions": ["1.0.0"]}'), io.StringIO('{"versions": ["1.0.1"]}')]
        with patch('urllib.request.urlopen', side_effect=replies) as get, patch('time.sleep') as sleep:
            self.verify_registry()
        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(20)

    def test_registry_verification_fails_after_bounded_polling(self):
        with patch('urllib.request.urlopen', side_effect=lambda *a, **kw: io.StringIO('{"versions": []}')) as get, patch('time.sleep') as sleep:
            with self.assertRaisesRegex(SystemExit, 'NuGet has not indexed the uploaded version'):
                self.verify_registry()
        self.assertEqual(get.call_count, 30)
        self.assertEqual(sleep.call_count, 30)

    def test_registry_verification_does_not_hide_non_404_errors(self):
        with patch('urllib.request.urlopen', side_effect=urllib.error.HTTPError('url', 401, 'unauthorized', {}, None)), patch('time.sleep') as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                self.verify_registry()
        sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
