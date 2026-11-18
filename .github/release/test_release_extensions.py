"""All tests are offline: fixtures, fake HTTP, fake gh. No publishing credentials."""
import importlib
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).parent))
import release_plan
import workflow_plan
import github_release
import complete_release
import release_notes


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

    def test_both_ruby_gems_are_inventoried_with_their_own_versions_and_tags(self):
        root = release_plan.REPO_ROOT
        auto_version = re.search(r'''^\s*s\.version\s*=\s*["']([^"']+)["']''',
                                 (root / 'ruby/traceai-auto.gemspec').read_text(), re.M).group(1)
        roast_version = re.search(r'''^\s*VERSION\s*=\s*["']([^"']+)["']''',
                                  (root / 'ruby/roast/lib/traceai/roast/version.rb').read_text(), re.M).group(1)
        gems = [p for p in release_plan.other_packages(root) if p['ecosystem'] == 'ruby']
        self.assertEqual([(p['name'], p['version'], p['dir'], p['tag']) for p in gems], [
            ('traceai-auto', auto_version, 'ruby', f'ruby-traceai-auto-v{auto_version}'),
            ('traceai-roast', roast_version, 'ruby/roast', f'ruby-traceai-roast-v{roast_version}'),
        ])

    def test_ruby_auto_has_release_please_version_file_and_manifest_entry(self):
        root = release_plan.REPO_ROOT
        config = json.loads((root / 'release-please-config.json').read_text())['packages']
        manifest = json.loads((root / '.release-please-manifest.json').read_text())
        # The component is the publisher's tag prefix (ruby-<gem>-v<version>), so release-please reads the
        # publisher-created tag as the release boundary, as the NuGet packages do.
        self.assertEqual(config['ruby'], {
            'release-type': 'simple', 'component': 'ruby-traceai-auto',
            'package-name': 'traceai-auto', 'extra-files': ['traceai-auto.gemspec'],
        })
        self.assertEqual(config['ruby/roast'], {
            'release-type': 'simple', 'component': 'ruby-traceai-roast',
            'package-name': 'traceai-roast', 'extra-files': ['lib/traceai/roast/version.rb'],
        })
        for path in ('ruby/traceai-auto.gemspec', 'ruby/roast/lib/traceai/roast/version.rb'):
            versioned = [line for line in (root / path).read_text().splitlines() if 'x-release-please-version' in line]
            self.assertEqual(len(versioned), 1, path)
        for gem in [p for p in release_plan.other_packages(root) if p['ecosystem'] == 'ruby']:
            component = config[gem['dir']]['component']
            self.assertEqual(gem['tag'], f"{component}-v{gem['version']}")
        version = re.search(r'''^\s*s\.version\s*=\s*["']([^"']+)["']''',
                            (root / 'ruby/traceai-auto.gemspec').read_text(), re.M).group(1)
        self.assertEqual(manifest['ruby'], version)
        gems = [p for p in release_plan.other_packages(root) if p['ecosystem'] == 'ruby']
        self.assertEqual(release_plan.coverage_problems(root, gems), [])

    def test_ruby_auto_version_is_read_from_the_gemspec_without_execution(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'ruby').mkdir()
            (root / 'ruby/traceai-auto.gemspec').write_text(
                'raise "gemspec must not execute"\nGem::Specification.new do |s|\n'
                '  s.name = "traceai-auto"\n  s.version = "0.9.7"\nend\n')
            gems = release_plan.other_packages(root)
            self.assertEqual([(p['name'], p['version'], p['tag']) for p in gems],
                             [('traceai-auto', '0.9.7', 'ruby-traceai-auto-v0.9.7')])

    def test_ruby_auto_missing_literal_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'ruby').mkdir()
            gemspec = root / 'ruby/traceai-auto.gemspec'
            for text in ('s.name = "traceai-auto"\n', 's.version = calculate_version()\n'):
                with self.subTest(gemspec=text):
                    gemspec.write_text(text)
                    with self.assertRaisesRegex(ValueError, 'missing Ruby gem version'):
                        release_plan.other_packages(root)

    def test_both_ruby_gems_are_planned_in_the_ruby_ecosystem_without_holds(self):
        gems = [p for p in release_plan.other_packages() if p['ecosystem'] == 'ruby']
        planned = release_plan.plan(gems, release_plan.load_config(), offline=True)
        self.assertEqual([p['name'] for p in planned['ruby']], ['traceai-auto', 'traceai-roast'])
        self.assertEqual(planned['skipped'], [])

    def test_ruby_registry_versions_use_the_public_versions_api(self):
        for name in ('traceai-auto', 'traceai-roast'):
            with self.subTest(name=name), patch.object(release_plan, '_get_json', return_value=[
                {'number': '0.1.0'}, {'number': '0.2.0'}, {'number': '0.2.0', 'platform': 'java'},
            ]) as get:
                self.assertEqual(release_plan.published_versions({'ecosystem': 'ruby', 'name': name}),
                                 {'0.1.0', '0.2.0'})
                get.assert_called_once_with(f'https://rubygems.org/api/v1/versions/{name}.json')

    def test_ruby_registry_404_is_unpublished_but_other_errors_raise(self):
        package = {'ecosystem': 'ruby', 'name': 'traceai-auto'}
        for status in (404, 401, 403, 429, 503):
            error = urllib.error.HTTPError('https://rubygems.org/api/v1/versions/traceai-auto.json', status, 'failed', {}, None)
            with self.subTest(status=status), patch('urllib.request.urlopen', side_effect=error):
                if status == 404:
                    self.assertIsNone(release_plan.published_versions(package))
                else:
                    with self.assertRaises(urllib.error.HTTPError):
                        release_plan.published_versions(package)

    def test_ruby_published_version_is_retained_only_for_reconciliation(self):
        package = {'ecosystem': 'ruby', 'name': 'traceai-auto', 'version': '0.1.0', 'dir': 'ruby',
                   'tag': 'ruby-traceai-auto-v0.1.0', 'core': False}
        with patch.object(release_plan, '_get_json', return_value=[{'number': '0.1.0'}]):
            planned = release_plan.plan([package], {}, include_published=True)
            self.assertTrue(planned['ruby'][0]['registry_published'])
            self.assertFalse(planned['ruby'][0]['first_release'])
            self.assertEqual(workflow_plan.unfinished(json.loads(json.dumps(planned)), set())['ruby'], planned['ruby'])
            self.assertEqual(workflow_plan.unfinished(planned, {package['tag']})['ruby'], [])
            skipped = release_plan.plan([package], {})
            self.assertEqual(skipped['ruby'], [])
            self.assertEqual(skipped['skipped'][0]['name'], 'traceai-auto')

    def test_go_auto_is_inventoried_and_released_with_its_own_tag(self):
        root = release_plan.REPO_ROOT
        auto = [p for p in release_plan.other_packages(root) if p['dir'] == 'go/auto']
        # Each release PR bumps the manifest version; the tag follows it.
        self.assertEqual([p['name'] for p in auto], ['github.com/future-agi/traceAI/go/auto'])
        self.assertEqual(auto[0]['tag'], f"go/auto/v{auto[0]['version']}")
        plan = release_plan.plan(auto, release_plan.load_config(), offline=True)
        self.assertEqual([p['name'] for p in plan['go']], ['github.com/future-agi/traceAI/go/auto'])
        self.assertEqual(plan['skipped'], [])

    def test_go_module_changelog_is_listed_only_when_the_module_has_one(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name in ('with', 'without'):
                mod = root / 'go' / name / 'go.mod'
                mod.parent.mkdir(parents=True)
                mod.write_text(f'module example.com/{name}\n\ngo 1.26.0\n')
            (root / 'go/with/CHANGELOG.md').write_text('# Changelog\n\n## [0.1.0] - 2026-11-18\n\n- First.\n')
            go = {p['dir']: p for p in release_plan.other_packages(root) if p['ecosystem'] == 'go'}
            self.assertEqual(go['go/with']['changelog'], 'go/with/CHANGELOG.md')
            self.assertIsNone(go['go/without']['changelog'])

    def test_go_auto_release_notes_come_from_its_own_changelog(self):
        root = release_plan.REPO_ROOT
        auto = [p for p in release_plan.other_packages(root) if p['dir'] == 'go/auto'][0]
        self.assertEqual(auto['changelog'], 'go/auto/CHANGELOG.md')
        text = (root / auto['changelog']).read_text()
        # The GitHub release for the manifest version must carry that version's section.
        self.assertTrue(release_notes.section(text, auto['version']))

    def test_java_agent_is_released_with_the_reactor(self):
        root = release_plan.REPO_ROOT
        java = [p for p in release_plan.other_packages(root) if p['ecosystem'] == 'java']
        self.assertEqual([p['name'] for p in java], ['com.futureagi:traceai-java-parent'])
        self.assertIn('traceai-javaagent', java[0]['artifacts'])
        plan = release_plan.plan(java, release_plan.load_config(), offline=True)
        # The agent is deployed with the rest of the reactor, so the Central check waits for it too.
        self.assertEqual([p['name'] for p in plan['java']], ['com.futureagi:traceai-java-parent'])
        planned = plan['java'][0]
        self.assertIn('traceai-javaagent', planned['artifacts'])
        self.assertIn('traceai-java-core', planned['artifacts'])
        self.assertEqual(planned['held_artifacts'], [])
        self.assertEqual([p for p in plan['skipped'] if p['name'] == 'com.futureagi:traceai-javaagent'], [])
        # No release profile turns Central publishing off for the agent.
        ns = {'m': 'http://maven.apache.org/POM/4.0.0'}
        pom = ET.parse(root / 'java/traceai-javaagent/pom.xml').getroot()
        skips = [p.findtext('m:configuration/m:skipPublishing', namespaces=ns)
                 for profile in pom.findall('m:profiles/m:profile', ns)
                 for p in profile.findall('m:build/m:plugins/m:plugin', ns)
                 if p.findtext('m:artifactId', namespaces=ns) == 'central-publishing-maven-plugin']
        self.assertEqual(skips, [])

    @staticmethod
    def _java_script(name):
        # Loaded inside each test so that a script that cannot be loaded fails that test only.
        import runpy
        return runpy.run_path(str(release_plan.REPO_ROOT / 'java/scripts' / name))

    def test_bom_names_every_released_module_and_no_held_one(self):
        root = release_plan.REPO_ROOT
        held = {name.split(':', 1)[1] for name in release_plan.load_config().get('hold_artifacts', {})}
        ns = {'m': 'http://maven.apache.org/POM/4.0.0'}
        bom = ET.parse(root / 'java/traceai-bom/pom.xml').getroot()
        managed = {d.findtext('m:artifactId', namespaces=ns) for d in bom.findall('m:dependencyManagement/m:dependencies/m:dependency', ns)}
        self.assertEqual(managed & held, set(), 'a released BOM must not name an artifact Central does not serve')
        self.assertIn('traceai-java-core', managed)
        self.assertIn('traceai-javaagent', managed, 'the released agent is aligned by the BOM')

    def test_central_check_expects_the_bom_to_leave_out_a_held_module_and_to_name_it_again_once_released(self):
        check = self._java_script('check_central.py')
        expected = {'traceai-java-parent', 'traceai-bom', 'traceai-java-core', 'traceai-javaagent'}
        hold = {'com.futureagi:traceai-javaagent': 'held'}
        self.assertEqual(check['bom_errors']({'traceai-java-core'}, expected, hold), [])
        self.assertTrue(check['bom_errors']({'traceai-java-core', 'traceai-javaagent'}, expected, hold), 'a held module in the BOM is an error')
        self.assertTrue(check['bom_errors']({'traceai-java-core'}, expected, {}), 'once the hold is lifted the BOM must name the module')
        self.assertEqual(check['bom_errors']({'traceai-java-core', 'traceai-javaagent'}, expected, {}), [])
        # The exception comes from the release hold list, not from a name written into the checker.
        self.assertEqual(check['held_artifact_ids'](release_plan.REPO_ROOT / '.github/release/config.json'), set())

    def test_jitpack_harvest_takes_every_module_including_the_released_agent(self):
        prepare_script = self._java_script('prepare_jitpack.py')
        root = release_plan.REPO_ROOT / 'java'
        ns = {'m': 'http://maven.apache.org/POM/4.0.0'}
        parent = ET.parse(root / 'pom.xml').getroot()
        version = parent.findtext('m:version', namespaces=ns)
        artifacts = [parent.findtext('m:artifactId', namespaces=ns)] + [
            ET.parse(root / m.text.strip() / 'pom.xml').getroot().findtext('m:artifactId', namespaces=ns) for m in parent.findall('m:modules/m:module', ns)]
        self.assertIn('traceai-javaagent', artifacts)

        def installed(repo, leave_out=()):
            # What a JitPack install leaves behind: a flattened POM for each module that was installed.
            for artifact in artifacts:
                if artifact in leave_out:
                    continue
                path = repo / 'com/futureagi' / artifact / version / f'{artifact}-{version}.pom'
                path.parent.mkdir(parents=True)
                path.write_text(f'<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>com.futureagi</groupId>'
                                f'<artifactId>{artifact}</artifactId><version>{version}</version></project>')

        with tempfile.TemporaryDirectory() as d:
            installed(Path(d))
            self.assertEqual(prepare_script['prepare'](Path(d), 'com.github.future-agi.traceAI', 'some-commit'), len(artifacts))
        with tempfile.TemporaryDirectory() as d:
            installed(Path(d), leave_out={'traceai-javaagent'})
            with self.assertRaises(OSError):
                prepare_script['prepare'](Path(d), 'com.github.future-agi.traceAI', 'some-commit')
        # The agent is installed on the JitPack route: no profile turns its install plugin off.
        agent = ET.parse(root / 'traceai-javaagent/pom.xml').getroot()
        skips = [p.findtext('m:configuration/m:skip', namespaces=ns)
                 for profile in agent.findall('m:profiles/m:profile', ns)
                 for p in profile.findall('m:build/m:plugins/m:plugin', ns)
                 if p.findtext('m:artifactId', namespaces=ns) == 'maven-install-plugin']
        self.assertEqual(skips, [])

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
        publishable_paths = {p.get('release_path', p['dir']) for p in packages}
        # Held adapter prototypes keep version metadata for eventual qualification,
        # but private:true must still exclude them from every publication plan.
        private_paths = {
            str(file.parent.relative_to(root)): json.loads(file.read_text())
            for file in (root / 'typescript/packages').glob('*/package.json')
            if json.loads(file.read_text()).get('private')
        }
        self.assertTrue(publishable_paths <= set(config['packages']))
        self.assertTrue(set(config['packages']) - publishable_paths <= set(private_paths))
        self.assertEqual(set(manifest), set(config['packages']))
        for path in set(config['packages']) - publishable_paths:
            self.assertEqual(manifest[path], private_paths[path]['version'])
            self.assertEqual(config['packages'][path]['release-type'], 'node')
            self.assertEqual(config['packages'][path]['package-name'], private_paths[path]['name'])
        for p in packages:
            self.assertEqual(manifest[p.get('release_path', p['dir'])], p['version'])


if __name__ == '__main__':
    unittest.main()
