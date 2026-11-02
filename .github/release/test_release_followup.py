"""Regression coverage for historical release completion and Java reconciliation."""
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
import complete_release
import release_plan


class ReleaseFollowupTests(unittest.TestCase):
    def test_pypi_empty_release_has_no_distribution(self):
        p = {'ecosystem': 'python', 'name': 'traceai-test'}
        with patch.object(release_plan, '_get_json', return_value={'releases': {'1.0.0': [], '1.0.1': [{'filename': 'a.whl'}]}}):
            self.assertEqual(release_plan.published_versions(p), {'1.0.1'})

    def test_java_parent_only_does_not_probe_modules(self):
        p = {'ecosystem': 'java', 'group': 'com.futureagi', 'version': '1.0.0', 'artifacts': ['parent', 'later']}
        response = Mock(); response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        with patch.object(release_plan.urllib.request, 'urlopen', side_effect=[response, TimeoutError('later module unavailable')]) as get:
            self.assertEqual(release_plan.published_versions(p, strict=False), {'1.0.0'})
            self.assertEqual(get.call_count, 1)

    def test_historical_java_release_requires_parent_not_new_module(self):
        p = {'ecosystem': 'java', 'name': 'com.futureagi:parent', 'group': 'com.futureagi',
             'version': '2.0.0', 'tag': 'java-v2.0.0', 'dir': 'java', 'artifacts': ['parent', 'new-module']}
        pending = [{'number': 7, 'headRefName': 'release-please--branches--main', 'mergeCommit': {'oid': 'm'}}]
        response = Mock(); response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo'}), \
             patch.object(complete_release, 'gh_json', side_effect=[pending, [[{'tag_name': 'java-v1.0.0'}]]]), \
             patch.object(complete_release, 'changed_release_paths', return_value={'java': '1.0.0'}), \
             patch.object(complete_release, 'python_packages', return_value=[]), \
             patch.object(complete_release, 'typescript_packages', return_value=[]), \
             patch.object(complete_release, 'other_packages', return_value=[p]), \
             patch.object(complete_release, 'load_config', return_value={'skip': {}}), \
             patch.object(release_plan.urllib.request, 'urlopen', side_effect=[response, TimeoutError('new module does not exist')]) as get, \
             patch('subprocess.run') as run:
            complete_release.main()
        self.assertEqual(get.call_count, 1)
        self.assertTrue(any(c.args[0][:3] == ['gh', 'pr', 'edit'] for c in run.call_args_list))

    def test_unknown_historical_path_remains_pending(self):
        p = {'ecosystem': 'python', 'name': 'a', 'version': '2.0.0', 'tag': 'a-v2.0.0', 'dir': 'python/a'}
        pending = [{'number': 7, 'headRefName': 'release-please--branches--main', 'mergeCommit': {'oid': 'm'}}]
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo'}), \
             patch.object(complete_release, 'gh_json', side_effect=[pending, [[]]]), \
             patch.object(complete_release, 'changed_release_paths', return_value={'python/removed': '1.0.0'}), \
             patch.object(complete_release, 'python_packages', return_value=[p]), \
             patch.object(complete_release, 'typescript_packages', return_value=[]), \
             patch.object(complete_release, 'other_packages', return_value=[]), \
             patch.object(complete_release, 'load_config', return_value={'skip': {}}), \
             patch('subprocess.run') as run:
            complete_release.main()
        run.assert_not_called()

    def test_one_registry_failure_does_not_block_other_pending_pr(self):
        packages = [{'ecosystem': 'python', 'name': name, 'version': '1.0.0', 'tag': f'{name}-v1.0.0', 'dir': f'python/{name}'} for name in ('a', 'b')]
        pending = [{'number': number, 'headRefName': 'release-please--branches--main', 'mergeCommit': {'oid': str(number)}} for number in (7, 8)]
        error = urllib.error.HTTPError('https://pypi.org', 503, 'unavailable', {}, None)
        def published(p, **kwargs):
            if p['name'] == 'a':
                raise error
            return {'1.0.0'}
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo'}), \
             patch.object(complete_release, 'gh_json', side_effect=[pending, [[{'tag_name': p['tag']} for p in packages]]]), \
             patch.object(complete_release, 'changed_release_paths', side_effect=lambda repo, pr: {f"python/{'a' if pr['number'] == 7 else 'b'}": '1.0.0'}), \
             patch.object(complete_release, 'python_packages', return_value=packages), \
             patch.object(complete_release, 'typescript_packages', return_value=[]), \
             patch.object(complete_release, 'other_packages', return_value=[]), \
             patch.object(complete_release, 'load_config', return_value={'skip': {}}), \
             patch.object(complete_release, 'published_versions', side_effect=published), \
             patch('subprocess.run') as run:
            complete_release.main()
        edits = [c.args[0][3] for c in run.call_args_list if c.args[0][:3] == ['gh', 'pr', 'edit']]
        self.assertEqual(edits, ['8'])

    def test_java_reconciliation_polls_parent_after_skipping_deploy(self):
        workflow = (Path(__file__).parents[1] / 'workflows/release-java.yml').read_text()
        self.assertIn("strict=not p['registry_published']", workflow)
