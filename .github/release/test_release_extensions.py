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

    def test_java_verification_needs_every_artifact_and_the_plan_only_the_parent(self):
        # Central deploys the reactor as one bundle, so the parent decides. A module missing at a
        # published version was added later and ships with the next version (Central refuses re-uploads).
        p = {'ecosystem': 'java', 'group': 'com.futureagi', 'version': '1.0.0', 'artifacts': ['parent', 'core']}
        context = Mock(); context.__enter__ = Mock(); context.__exit__ = Mock(return_value=False)
        with patch('urllib.request.urlopen', side_effect=[context, urllib.error.HTTPError('url', 404, 'missing', {}, None)]):
            self.assertIsNone(release_plan.published_versions(p))
        with patch('urllib.request.urlopen', side_effect=[context, urllib.error.HTTPError('url', 404, 'missing', {}, None)]):
            self.assertEqual(release_plan.published_versions(p, strict=False), {'1.0.0'})
        with patch('urllib.request.urlopen', side_effect=[urllib.error.HTTPError('url', 404, 'missing', {}, None)]) as get:
            self.assertIsNone(release_plan.published_versions(p))
            self.assertEqual(get.call_count, 1)
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
        package = {'ecosystem': 'go', 'name': 'example/module', 'version': '0.1.0', 'tag': 'go/module/v0.1.0', 'dir': 'go/module'}
        pending = [{'number': 1, 'headRefName': 'release-please--branches--main', 'mergeCommit': {'oid': 'm'}}]
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo'}), patch.object(complete_release, 'gh_json', side_effect=[pending, [[{'tag_name': package['tag']}]]]), patch.object(complete_release, 'changed_release_paths', return_value={'go/module': '0.1.0'}), patch.object(complete_release, 'python_packages', return_value=[]), patch.object(complete_release, 'typescript_packages', return_value=[]), patch.object(complete_release, 'other_packages', return_value=[package]), patch.object(complete_release, 'load_config', return_value={'skip': {}}), patch.object(complete_release, 'published_versions', return_value=None), patch('subprocess.run') as run:
            complete_release.main()
            run.assert_not_called()

    def test_completion_requires_only_the_versions_the_release_pr_changed(self):
        # A first-time package still waiting on its registry must not block an unrelated release PR.
        released = {'ecosystem': 'python', 'name': 'a', 'version': '0.1.1', 'tag': 'a-v0.1.1', 'dir': 'python/frameworks/a'}
        waiting = {'ecosystem': 'python', 'name': 'b', 'version': '0.1.0', 'tag': 'b-v0.1.0', 'dir': 'python/frameworks/b'}
        pending = [{'number': 7, 'headRefName': 'release-please--branches--main', 'mergeCommit': {'oid': 'm'}}]
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo'}), \
             patch.object(complete_release, 'gh_json', side_effect=[pending, [[{'tag_name': 'a-v0.1.1'}]]]), \
             patch.object(complete_release, 'changed_release_paths', return_value={'python/frameworks/a': '0.1.1'}), \
             patch.object(complete_release, 'python_packages', return_value=[released, waiting]), \
             patch.object(complete_release, 'typescript_packages', return_value=[]), \
             patch.object(complete_release, 'other_packages', return_value=[]), \
             patch.object(complete_release, 'load_config', return_value={'skip': {}}), \
             patch.object(complete_release, 'published_versions', return_value={'0.1.1'}), \
             patch('subprocess.run') as run:
            complete_release.main()
        commands = [c.args[0] for c in run.call_args_list]
        self.assertIn(['gh', 'pr', 'edit', '7', '--repo', 'owner/repo', '--remove-label', 'autorelease: pending', '--add-label', 'autorelease: tagged'], commands)
        self.assertTrue(any(c[:3] == ['gh', 'label', 'create'] for c in commands), 'the tagged label is created when missing')

    def test_completion_still_waits_for_a_version_the_release_pr_changed(self):
        changed = {'ecosystem': 'python', 'name': 'a', 'version': '0.1.1', 'tag': 'a-v0.1.1', 'dir': 'python/frameworks/a'}
        pending = [{'number': 7, 'headRefName': 'release-please--branches--main', 'mergeCommit': {'oid': 'm'}}]
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo'}), \
             patch.object(complete_release, 'gh_json', side_effect=[pending, [[]]]), \
             patch.object(complete_release, 'changed_release_paths', return_value={'python/frameworks/a': '0.1.1'}), \
             patch.object(complete_release, 'python_packages', return_value=[changed]), \
             patch.object(complete_release, 'typescript_packages', return_value=[]), \
             patch.object(complete_release, 'other_packages', return_value=[]), \
             patch.object(complete_release, 'load_config', return_value={'skip': {}}), \
             patch('subprocess.run') as run:
            complete_release.main()
        run.assert_not_called()

    def test_completion_checks_the_released_version_not_the_current_checkout(self):
        # main has moved on to 0.1.2; the pending release PR released 0.1.1.
        package = {'ecosystem': 'python', 'name': 'a', 'version': '0.1.2', 'tag': 'a-v0.1.2', 'dir': 'python/frameworks/a'}
        pending = [{'number': 7, 'headRefName': 'release-please--branches--main', 'mergeCommit': {'oid': 'm'}}]
        seen = []
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo'}), \
             patch.object(complete_release, 'gh_json', side_effect=[pending, [[{'tag_name': 'a-v0.1.1'}]]]), \
             patch.object(complete_release, 'changed_release_paths', return_value={'python/frameworks/a': '0.1.1'}), \
             patch.object(complete_release, 'python_packages', return_value=[package]), \
             patch.object(complete_release, 'typescript_packages', return_value=[]), \
             patch.object(complete_release, 'other_packages', return_value=[]), \
             patch.object(complete_release, 'load_config', return_value={'skip': {}}), \
             patch.object(complete_release, 'published_versions', side_effect=lambda p: seen.append(p['version']) or {'0.1.1'}), \
             patch('subprocess.run') as run:
            complete_release.main()
        self.assertEqual(seen, ['0.1.1'])
        self.assertTrue(any(c.args[0][:3] == ['gh', 'pr', 'edit'] for c in run.call_args_list))

    def test_each_pending_release_pr_is_completed_on_its_own_versions(self):
        # Two pending release PRs for the same package: only the one whose version is released completes.
        package = {'ecosystem': 'python', 'name': 'a', 'version': '0.1.2', 'tag': 'a-v0.1.2', 'dir': 'python/frameworks/a'}
        pending = [{'number': 9, 'headRefName': 'release-please--branches--main', 'mergeCommit': {'oid': 'n'}},
                   {'number': 8, 'headRefName': 'release-please--branches--main', 'mergeCommit': {'oid': 'o'}}]
        changed = {9: {'python/frameworks/a': '0.1.2'}, 8: {'python/frameworks/a': '0.1.1'}}
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo'}), \
             patch.object(complete_release, 'gh_json', side_effect=[pending, [[{'tag_name': 'a-v0.1.1'}]]]), \
             patch.object(complete_release, 'changed_release_paths', side_effect=lambda repo, pr: changed[pr['number']]), \
             patch.object(complete_release, 'python_packages', return_value=[package]), \
             patch.object(complete_release, 'typescript_packages', return_value=[]), \
             patch.object(complete_release, 'other_packages', return_value=[]), \
             patch.object(complete_release, 'load_config', return_value={'skip': {}}), \
             patch.object(complete_release, 'published_versions', return_value={'0.1.1'}), \
             patch('subprocess.run') as run:
            complete_release.main()
        edits = [c.args[0][3] for c in run.call_args_list if c.args[0][:3] == ['gh', 'pr', 'edit']]
        self.assertEqual(edits, ['8'])

    def test_completion_keeps_a_release_pr_pending_when_its_versions_cannot_be_read(self):
        # #8 released 0.1.1, which never shipped; its lookup fails while #9's 0.1.2 is released.
        package = {'ecosystem': 'python', 'name': 'a', 'version': '0.1.2', 'tag': 'a-v0.1.2', 'dir': 'python/frameworks/a'}
        pending = [{'number': 9, 'headRefName': 'release-please--branches--main', 'mergeCommit': {'oid': 'n'}},
                   {'number': 8, 'headRefName': 'release-please--branches--main', 'mergeCommit': {'oid': 'o'}}]
        changed = {9: {'python/frameworks/a': '0.1.2'}, 8: None}
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo'}), \
             patch.object(complete_release, 'gh_json', side_effect=[pending, [[{'tag_name': 'a-v0.1.2'}]]]), \
             patch.object(complete_release, 'changed_release_paths', side_effect=lambda repo, pr: changed[pr['number']]), \
             patch.object(complete_release, 'python_packages', return_value=[package]), \
             patch.object(complete_release, 'typescript_packages', return_value=[]), \
             patch.object(complete_release, 'other_packages', return_value=[]), \
             patch.object(complete_release, 'load_config', return_value={'skip': {}}), \
             patch.object(complete_release, 'published_versions', return_value={'0.1.2'}), \
             patch('subprocess.run') as run:
            complete_release.main()
        edits = [c.args[0][3] for c in run.call_args_list if c.args[0][:3] == ['gh', 'pr', 'edit']]
        self.assertEqual(edits, ['9'])

    def test_with_version_rewrites_every_tag_style(self):
        for tag, version, want in (('traceai-a-v0.1.2', '0.1.1', 'traceai-a-v0.1.1'), ('npm-traceai-x-v1.2.0', '1.1.9', 'npm-traceai-x-v1.1.9'),
                                   ('java-v1.0.2', '1.0.1', 'java-v1.0.1'), ('go/openai/v0.1.2', '0.1.1', 'go/openai/v0.1.1')):
            self.assertEqual(complete_release.with_version({'tag': tag, 'version': tag.rsplit('v', 1)[1]}, version)['tag'], want)

    def test_changed_release_paths_compares_the_manifest_across_the_merge(self):
        import base64 as b64
        enc = lambda d: {'content': b64.b64encode(json.dumps(d).encode()).decode()}
        replies = {'repos/o/r/commits/m': {'parents': [{'sha': 'p'}]},
                   'repos/o/r/contents/.release-please-manifest.json?ref=p': enc({'x': '1.0.0', 'y': '0.1.0'}),
                   'repos/o/r/contents/.release-please-manifest.json?ref=m': enc({'x': '1.0.1', 'y': '0.1.0', 'z': '0.1.0'})}
        with patch.object(complete_release, 'gh_json', side_effect=lambda *a: replies[a[1]]):
            self.assertEqual(complete_release.changed_release_paths('o/r', {'mergeCommit': {'oid': 'm'}}), {'x': '1.0.1', 'z': '0.1.0'})
        self.assertIsNone(complete_release.changed_release_paths('o/r', {'number': 1}))

    def test_java_new_module_does_not_republish_a_published_reactor(self):
        import urllib.error as ue
        p = {'ecosystem': 'java', 'name': 'com.futureagi:traceai-java-parent', 'group': 'com.futureagi', 'version': '1.0.0',
             'artifacts': ['traceai-java-parent', 'traceai-java-core', 'traceai-java-new']}  # parent first, as other_packages builds it
        def urlopen(url, timeout=30):
            if 'traceai-java-new' in url:
                raise ue.HTTPError(url, 404, 'not found', {}, None)
            return Mock(__enter__=lambda s: s, __exit__=lambda *a: False)
        with patch.object(release_plan.urllib.request, 'urlopen', side_effect=urlopen):
            self.assertEqual(release_plan.published_versions(p, strict=False), {'1.0.0'}, 'the plan does not re-deploy 1.0.0')
            self.assertIsNone(release_plan.published_versions(p), 'verification still needs every artifact')
            planned = release_plan.plan([{**p, 'core': False}], {})
            self.assertEqual(planned['java'], [])
            self.assertIn('1.0.0 is already published', planned['skipped'][0]['reason'])
        def missing_parent(url, timeout=30):
            raise ue.HTTPError(url, 404, 'not found', {}, None)
        with patch.object(release_plan.urllib.request, 'urlopen', side_effect=missing_parent):
            self.assertIsNone(release_plan.published_versions(p))

    def test_workflow_plan_orders_every_npm_dependency_edge(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            data = {eco: [] for eco in release_plan.ECOSYSTEMS}
            # Mastra is alphabetically ahead of Vercel, and is a reconciliation
            # entry, not an upload. It must still wait for Vercel's new version.
            data.update(skipped=[], typescript=[
                {'name': '@traceai/mastra', 'workspace_dependencies': ['@traceai/fi-core', '@traceai/vercel'], 'registry_published': True},
                {'name': '@traceai/vercel', 'workspace_dependencies': ['@traceai/fi-semantic-conventions'], 'registry_published': False},
                {'name': '@traceai/fi-core', 'workspace_dependencies': ['@traceai/fi-semantic-conventions']},
                {'name': '@traceai/fi-semantic-conventions', 'workspace_dependencies': []}])
            for p in data['typescript']:
                p.update(version='1.0.0', tag=p['name'])
            (root / 'plan.json').write_text(json.dumps(data)); (root / 'tags.txt').write_text('')
            with patch.dict(os.environ, {'GITHUB_OUTPUT': str(root / 'output'), 'GITHUB_STEP_SUMMARY': str(root / 'summary')}), patch.object(sys, 'argv', ['script', str(root / 'plan.json'), str(root / 'tags.txt')]):
                workflow_plan.main()
            outputs = dict(line.split('=', 1) for line in (root / 'output').read_text().splitlines())
            names = [p['name'] for p in json.loads(outputs['typescript'])]
            for p in data['typescript']:
                for dep in p['workspace_dependencies']:
                    self.assertLess(names.index(dep), names.index(p['name']))
            workflow = (release_plan.REPO_ROOT / '.github/workflows/release-typescript.yml').read_text()
            self.assertNotIn('matrix:', workflow, 'a matrix would ignore the sorted list order')
            self.assertIn('npm_release.py publish', workflow)

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
