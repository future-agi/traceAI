"""No registry writes. Real offline npm pack/install plus DAG/delivery regressions."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import npm_release
import release_plan


class NpmTests(unittest.TestCase):
    def test_real_graph_orders_vercel_before_mastra_and_all_other_edges(self):
        packages = release_plan.typescript_packages()
        ordered = [p['name'] for p in npm_release.topological(packages)]
        self.assertLess(ordered.index('@traceai/vercel'), ordered.index('@traceai/mastra'))
        for package in packages:
            for dep in package['workspace_dependencies']:
                self.assertLess(ordered.index(dep), ordered.index(package['name']))

    def test_internal_semver_optional_and_peer_edges_are_detected_not_dev_edges(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name, fields in [('a', {'dependencies': {'b': '^1.0.0'}, 'optionalDependencies': {'c': 'workspace:*'},
                                      'peerDependencies': {'d': '^1.0.0'}, 'devDependencies': {'e': 'workspace:*'}}),
                                 *[(name, {}) for name in 'bcde']]:
                directory = root / 'typescript/packages' / name
                directory.mkdir(parents=True)
                (directory / 'package.json').write_text(json.dumps({'name': name, 'version': '1.0.0', **fields}))
            self.assertEqual(release_plan.typescript_packages(root)[0]['workspace_dependencies'], ['b', 'c', 'd'])

    def test_arbitrary_depth_cycle_and_duplicate_plans(self):
        packages = [{'name': n, 'workspace_dependencies': [dep] if dep else []}
                    for n, dep in [('d', 'c'), ('b', 'a'), ('a', None), ('c', 'b')]]
        self.assertEqual([p['name'] for p in npm_release.topological(packages)], list('abcd'))
        packages[2]['workspace_dependencies'] = ['d']
        with self.assertRaisesRegex(ValueError, 'Cyclic'):
            npm_release.topological(packages)
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            npm_release.topological([{'name': 'a'}, {'name': 'a'}])

    def test_consumer_only_plan_stages_transitive_dependencies_and_checks_versions(self):
        packages = [{'name': n, 'version': '9.9.9', 'workspace_dependencies': [dep] if dep else []}
                    for n, dep in [('core', 'semantic'), ('semantic', None), ('consumer', 'core')]]
        closure = npm_release.dependency_closure([packages[-1]], packages)
        self.assertEqual([p['name'] for p in closure], ['semantic', 'core', 'consumer'])
        with self.assertRaisesRegex(ValueError, 'differs'):
            npm_release.dependency_closure([{**packages[-1], 'version': '1.0.0'}], packages)
        with self.assertRaisesRegex(ValueError, 'not a publishable'):
            npm_release.dependency_closure([{'name': 'missing'}], packages)

    @unittest.skipUnless(shutil.which('npm') and shutil.which('node'), 'offline integration requires Node/npm')
    def test_real_offline_clean_install_unpublished_transitive_workspace_tarballs(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            packages = []
            # These deliberately unpublished names/versions can never come from
            # the network: --offline plus an empty npm cache enforce that.
            specs = [('semantic', []), ('core', ['semantic']), ('vercel', ['semantic']), ('consumer', ['core', 'vercel'])]
            env = {**os.environ, 'npm_config_cache': str(root / 'empty-cache'), 'npm_config_update_notifier': 'false'}
            for name, dependencies in specs:
                full = '@traceai-release-test/' + name
                source = root / 'typescript/packages' / name
                source.mkdir(parents=True)
                manifest = {'name': full, 'version': '999.0.0', 'main': 'index.js',
                            'dependencies': {'@traceai-release-test/' + dep: '^999.0.0' for dep in dependencies}}
                (source / 'package.json').write_text(json.dumps(manifest))
                (source / 'index.js').write_text('exports.ok = true;\n' + '\n'.join(f'require("@traceai-release-test/{dep}");' for dep in dependencies))
                package = {'name': full, 'version': '999.0.0'}
                dest = npm_release.tarball_dir(root / 'dist', package)
                dest.mkdir(parents=True)
                subprocess.run(['npm', 'pack', '--offline', '--ignore-scripts', '--pack-destination', str(dest)],
                               cwd=source, env=env, check=True, capture_output=True)
                packages.append(package)
            with patch.dict(os.environ, env):
                # Only consumer is requested, but both core and the non-core
                # dependency Vercel must be installed from the staged artifacts.
                npm_release.verify(root, [packages[-1]], root / 'dist', root / 'smoke', offline=True)
            installed = npm_release.tarball_dir(root / 'smoke', packages[-1]) / 'node_modules'
            for package in packages:
                self.assertEqual(json.loads((installed / package['name'] / 'package.json').read_text())['version'], '999.0.0')

    def test_verify_skips_versions_already_on_npm(self):
        # A published version is never uploaded again, so a defect in it must not block other uploads.
        packages = [{'name': 'published', 'version': '1.0.0', 'registry_published': True},
                    {'name': 'fresh', 'version': '1.0.0', 'registry_published': False}]
        commands = []

        def run(cmd, cwd, **kw):
            commands.append(cmd)
            if cmd[:2] == ['npm', 'install']:
                (cwd / 'node_modules/fresh').mkdir(parents=True)
                (cwd / 'node_modules/fresh/package.json').write_text('{"version": "1.0.0"}')

        with tempfile.TemporaryDirectory() as d, \
             patch.object(npm_release, 'typescript_packages', return_value=packages), \
             patch.object(npm_release, 'tarball', side_effect=lambda dist, p: dist / (p['name'] + '.tgz')), \
             patch.object(npm_release.subprocess, 'run', side_effect=run):
            npm_release.verify(Path('/fixture'), packages, Path('/dist'), Path(d))
            self.assertFalse((Path(d) / 'published').exists())
        self.assertEqual([c[-1] for c in commands if c[:2] == ['npm', 'install']], ['/dist/fresh.tgz'])
        self.assertEqual(sum(c[0] == 'node' for c in commands), 2, 'CJS require and ESM import of the fresh package')

    def test_publish_waits_for_every_dependency_before_uploading_consumer(self):
        packages = [{'name': n, 'version': '1.0.0', 'workspace_dependencies': deps, 'tag': n,
                     'registry_published': n == 'consumer'}
                    for n, deps in [('consumer', ['vercel', 'core']), ('vercel', []), ('core', [])]]
        events = []
        with patch.object(npm_release, 'typescript_packages', return_value=packages), \
             patch.object(npm_release, 'wait_until_served', side_effect=lambda n, v: events.append(('served', n))), \
             patch.object(npm_release, 'tarball', side_effect=lambda dist, p: dist / (p['name'] + '.tgz')), \
             patch.object(npm_release.subprocess, 'run', side_effect=lambda cmd, **kw: events.append(('publish' if cmd[1] == 'publish' else 'release', cmd[2]))):
            npm_release.publish(Path('/fixture'), packages, Path('/dist'))
        self.assertLess(events.index(('served', 'vercel')), events.index(('served', 'consumer')))
        self.assertLess(events.index(('served', 'core')), events.index(('served', 'consumer')))
        self.assertNotIn(('publish', '/dist/consumer.tgz'), events, 'reconciliation never reuploads')
        self.assertEqual([e[1] for e in events if e[0] == 'publish'], ['/dist/core.tgz', '/dist/vercel.tgz'])

    def test_omitted_unpublished_dependency_blocks_delivery_before_upload(self):
        inventory = [{'name': 'consumer', 'version': '1.0.0', 'workspace_dependencies': ['core']},
                     {'name': 'core', 'version': '1.0.0'}]
        with patch.object(npm_release, 'typescript_packages', return_value=inventory), \
             patch.object(npm_release, 'wait_until_served', side_effect=RuntimeError('not served')), \
             patch.object(npm_release.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'not served'):
                npm_release.publish(Path('/fixture'), [inventory[0]], Path('/dist'))
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
