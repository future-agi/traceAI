"""All tests are offline: fixtures, fake HTTP, fake gh. No publishing credentials."""
import importlib
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).parent))
import release_plan
import workflow_plan
import github_release
import complete_release


class ExtensionTests(unittest.TestCase):
    def test_python_discovery_includes_nested_plugin_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            p = root / 'python/frameworks/hermes/plugin/futureagi/pyproject.toml'
            p.parent.mkdir(parents=True)
            p.write_text('[project]\nname="futureagi-hermes-plugin"\nversion="0.1.0"\n')
            packages = release_plan.python_packages(root)
            self.assertEqual(len(packages), 1)
            self.assertEqual(packages[0]['dir'], 'python/frameworks/hermes/plugin/futureagi')

    def test_nested_runtime_version_is_checked(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'fi_instrumentation/instrumentation/version.py'
            p.parent.mkdir(parents=True)
            p.write_text('__version__ = "1.2.1"\n')
            self.assertEqual(release_plan.declared_versions(Path(d), 'fi_instrumentation'),
                             {'fi_instrumentation/instrumentation/version.py': '1.2.1'})

    def test_go_discovery_uses_manifest_not_hardcoded_initial_after_first_release(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'go/openai').mkdir(parents=True)
            (root / 'go/openai/go.mod').write_text('module github.com/future-agi/traceAI/go/openai\n\ngo 1.26.0\n')
            (root / '.release-please-manifest.json').write_text('{"go/openai":"0.1.7"}')
            p = release_plan.other_packages(root)[0]
            self.assertEqual((p['version'], p['tag']), ('0.1.7', 'go/openai/v0.1.7'))

    def test_go_proxy_case_escaping(self):
        with patch.object(release_plan, '_get_json', return_value={'Version': 'v0.1.0'}) as get:
            p = {'ecosystem': 'go', 'name': 'github.com/future-agi/traceAI/go/openai', 'version': '0.1.0'}
            self.assertEqual(release_plan.published_versions(p), {'0.1.0'})
            self.assertEqual(get.call_args.args[0], 'https://proxy.golang.org/github.com/future-agi/trace!a!i/go/openai/@v/v0.1.0.info')

    def test_registry_error_is_not_treated_as_unpublished(self):
        for status in (401, 403, 429, 503):
            error = urllib.error.HTTPError('https://example.com', status, 'failed', {}, None)
            with patch('urllib.request.urlopen', side_effect=error):
                with self.assertRaises(urllib.error.HTTPError):
                    release_plan._get_json('https://example.com')

    def test_registry_404_means_missing(self):
        with patch('urllib.request.urlopen', side_effect=urllib.error.HTTPError('https://example.com', 404, 'missing', {}, None)):
            self.assertIsNone(release_plan._get_json('https://example.com'))

    def test_java_cannot_publish_legacy_or_snapshot_coordinates(self):
        p = {'ecosystem': 'java', 'name': 'ai.traceai:parent', 'group': 'ai.traceai', 'version': '1.0.0', 'core': False}
        self.assertEqual(release_plan.plan([p], {}, offline=True)['java'], [])
        p.update(group='com.futureagi', version='1.1.0-SNAPSHOT')
        self.assertEqual(release_plan.plan([p], {}, offline=True)['java'], [])

    def test_java_skip_requires_every_reactor_artifact_on_central(self):
        p = {'ecosystem': 'java', 'group': 'com.futureagi', 'version': '1.0.0', 'artifacts': ['parent', 'core']}
        context = Mock(); context.__enter__ = Mock(); context.__exit__ = Mock(return_value=False)
        with patch('urllib.request.urlopen', side_effect=[context, urllib.error.HTTPError('url', 404, 'missing', {}, None)]):
            self.assertIsNone(release_plan.published_versions(p))
        with patch('urllib.request.urlopen', return_value=context) as get:
            self.assertEqual(release_plan.published_versions(p), {'1.0.0'})
            self.assertEqual(get.call_count, 2)

    def test_nuget_registry_name_is_lowercase(self):
        with patch.object(release_plan, '_get_json', return_value={'versions': ['1.0.0']}) as get:
            self.assertEqual(release_plan.published_versions({'ecosystem': 'nuget', 'name': 'FI-Instrumentation-Otel'}), {'1.0.0'})
            self.assertIn('/fi-instrumentation-otel/', get.call_args.args[0])

    def test_published_version_remains_for_tag_retry_but_will_not_reupload(self):
        p = {'ecosystem': 'python', 'name': 'a', 'dir': 'python/a', 'version': '1.0.0', 'core': False, 'tag': 'a-v1.0.0'}
        with patch.object(release_plan, 'published_versions', return_value={'1.0.0'}):
            result = release_plan.plan([p], {}, include_published=True)
        self.assertTrue(result['python'][0]['registry_published'])
        self.assertEqual(workflow_plan.unfinished(json.loads(json.dumps(result)), set())['python'], result['python'])
        self.assertEqual(workflow_plan.unfinished(result, {'a-v1.0.0'})['python'], [])

    def test_missing_registry_version_never_skipped_because_a_tag_exists(self):
        p = {'tag': 'a-v1.0.0', 'registry_published': False}
        data = {eco: [] for eco in release_plan.ECOSYSTEMS}
        data['python'] = [p]
        self.assertEqual(workflow_plan.unfinished(data, {'a-v1.0.0'})['python'], [p])

    def test_existing_github_release_is_not_recreated(self):
        with patch.dict(os.environ, {'TAG': 'tag', 'NAME': 'a', 'VERSION': '1.0.0'}), patch.object(sys, 'argv', ['script', '--install', 'pip install a']), patch('subprocess.run', return_value=Mock(returncode=0)) as run:
            github_release.main()
            self.assertEqual(run.call_count, 1)

    def test_registry_existing_release_does_not_attach_newly_built_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'a.whl').write_text('not the old registry wheel')
            env = {'TAG': 'tag', 'NAME': 'a', 'VERSION': '1.0.0', 'RUNNER_TEMP': d, 'GITHUB_SHA': 'a' * 40, 'REGISTRY_PUBLISHED': 'true'}
            with patch.dict(os.environ, env), patch.object(sys, 'argv', ['script', '--install', 'pip install a', '--dist', d]), patch('subprocess.run', side_effect=[Mock(returncode=1), Mock(returncode=0)]) as run:
                github_release.main()
                command = run.call_args.args[0]
                self.assertNotIn(str(Path(d) / 'a.whl'), command)
                self.assertIn('--latest=false', command)

    def test_completion_only_considers_merged_release_please_branches(self):
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo'}), patch.object(complete_release, 'gh_json', return_value=[{'number': 1, 'headRefName': 'some-feature'}]) as gh:
            complete_release.main()
            self.assertEqual(gh.call_count, 1)

    def test_completion_does_not_acknowledge_go_tag_until_proxy_serves_version(self):
        package = {'ecosystem': 'go', 'name': 'example/module', 'version': '0.1.0', 'tag': 'go/module/v0.1.0'}
        pending = [{'number': 1, 'headRefName': 'release-please--branches--main'}]
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo'}), patch.object(complete_release, 'gh_json', side_effect=[pending, [[{'tag_name': package['tag']}]]]), patch.object(complete_release, 'python_packages', return_value=[]), patch.object(complete_release, 'typescript_packages', return_value=[]), patch.object(complete_release, 'other_packages', return_value=[package]), patch.object(complete_release, 'load_config', return_value={'skip': {}}), patch.object(complete_release, 'published_versions', return_value=None), patch('subprocess.run') as run:
            complete_release.main()
            run.assert_not_called()

    def test_workflow_plan_serializes_npm_semantic_before_core(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            data = {eco: [] for eco in release_plan.ECOSYSTEMS}
            data.update(skipped=[], typescript=[{'name': '@traceai/fi-core', 'core': True, 'version': '1.0.0', 'tag': 'core'}, {'name': '@traceai/fi-semantic-conventions', 'core': True, 'version': '1.0.0', 'tag': 'semantic'}])
            (root / 'plan.json').write_text(json.dumps(data)); (root / 'tags.txt').write_text('')
            with patch.dict(os.environ, {'GITHUB_OUTPUT': str(root / 'output'), 'GITHUB_STEP_SUMMARY': str(root / 'summary')}), patch.object(sys, 'argv', ['script', str(root / 'plan.json'), str(root / 'tags.txt')]):
                workflow_plan.main()
            outputs = dict(line.split('=', 1) for line in (root / 'output').read_text().splitlines())
            self.assertEqual([p['name'] for p in json.loads(outputs['typescript_semantic'])], ['@traceai/fi-semantic-conventions'])
            self.assertEqual([p['name'] for p in json.loads(outputs['typescript_core'])], ['@traceai/fi-core'])

    def test_real_repo_has_complete_manifest_and_runtime_markers(self):
        root = release_plan.REPO_ROOT
        packages = release_plan.python_packages(root) + release_plan.typescript_packages(root) + release_plan.other_packages(root)
        self.assertEqual(release_plan.consistency_problems(packages), [])
        self.assertEqual(release_plan.coverage_problems(root, packages), [])
        config = json.loads((root / 'release-please-config.json').read_text())
        manifest = json.loads((root / '.release-please-manifest.json').read_text())
        self.assertEqual(set(config['packages']), {p.get('release_path', p['dir']) for p in packages})
        self.assertEqual(set(manifest), set(config['packages']))
        for p in packages:
            self.assertEqual(manifest[p.get('release_path', p['dir'])], p['version'])


if __name__ == '__main__':
    unittest.main()
