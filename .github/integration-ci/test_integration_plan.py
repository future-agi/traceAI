"""Planner regression checks: every new path, shared dependencies and safe skips."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from integration_plan import TARGETS, WORKFLOW, PlanError, plan, changed_paths


class IntegrationPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tests = {}
        for name, language, directory, manifest in TARGETS:
            package = self.root / directory
            package.mkdir(parents=True)
            (package / manifest).write_text('{}')
            test = {'typescript': 'src/__tests__/tracer.test.ts', 'go': 'tracer_test.go',
                    'java': 'src/test/java/example/ExportTest.java'}[language]
            test_path = package / test
            test_path.parent.mkdir(parents=True, exist_ok=True)
            test_path.touch()
            self.tests[name] = test_path

    def names(self, paths, **kwargs):
        return {p['name'] for matrix in plan(paths, self.root, **kwargs).values() for p in matrix}

    def test_every_owned_path_selects_its_own_target(self):
        for name, _, directory, manifest in TARGETS:
            for suffix in [manifest, 'README.md', 'example/main.go', 'src/changed.ts']:
                with self.subTest(name=name, suffix=suffix):
                    self.assertEqual(self.names([directory + '/' + suffix]), {name})

    def test_ci_edits_and_all_select_every_present_target(self):
        expected = {t[0] for t in TARGETS}
        for path in [WORKFLOW, '.github/integration-ci/integration_plan.py',
                     '.github/integration-ci/test_integration_plan.py',
                     '.github/integration-ci/install_maven.sh', '.github/scripts/python_test_plan.py']:
            self.assertEqual(self.names([path]), expected)
        self.assertEqual(self.names([], all_targets=True), expected)

    def test_shared_typescript_inputs_cover_both_consumers(self):
        for path in ['typescript/pnpm-lock.yaml', 'typescript/package.json',
                     'typescript/pnpm-workspace.yaml', 'typescript/.npmrc',
                     'typescript/tsconfig.base.json', 'typescript/packages/fi-core/src/index.ts',
                     'typescript/packages/fi-semantic-conventions/src/index.ts']:
            with self.subTest(path=path):
                self.assertEqual(self.names([path]), {'codex-sdk', 'pi-coding-agent'})

    def test_unrelated_paths_do_not_claim_coverage(self):
        self.assertEqual(self.names(['README.md', 'python/frameworks/openai/tracer.py',
                                    '.github/workflows/python-tests.yml',
                                    'typescript/packages/traceai_openai/src/index.ts',
                                    'java/traceai-core/pom.xml', 'go/openai-other/main.go']), set())

    def test_target_not_yet_landed_is_ignored(self):
        for _, _, directory, _ in TARGETS[1:]:
            shutil.rmtree(self.root / directory)
        self.assertEqual(self.names([WORKFLOW]), {'codex-sdk'})
        self.assertEqual(self.names(['go/a2a/interceptor.go']), set())

    def test_entirely_deleted_target_is_ignored(self):
        shutil.rmtree(self.root / 'go/openai')
        self.assertEqual(self.names(['go/openai/go.mod']), set())

    def test_missing_manifest_fails_closed(self):
        (self.root / 'go/openai/go.mod').unlink()
        with self.assertRaisesRegex(PlanError, 'missing go.mod'):
            plan(['go/openai/tracer.go'], self.root)

    def test_no_tests_fails_closed_for_each_language(self):
        for name in ['codex-sdk', 'openai', 'arconia']:
            with self.subTest(name=name):
                self.tests[name].unlink()
                with self.assertRaisesRegex(PlanError, 'no tests'):
                    plan([WORKFLOW], self.root)
                self.tests[name].touch()

    def test_duplicate_paths_do_not_duplicate_jobs(self):
        result = plan(['go/openai/go.mod'] * 5, self.root)
        self.assertEqual(len(result['go']), 1)

    def test_moves_plan_both_old_and_new_owner_using_existing_diff_reader(self):
        # Exercise real git's --no-renames/-z path handling, not just an artificial list.
        def git(*args):
            return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.STDOUT)
        git('init', '-q')
        git('add', '.')
        env = __import__('os').environ.copy()
        env.update(GIT_AUTHOR_NAME='CI test', GIT_COMMITTER_NAME='CI test',
                   GIT_AUTHOR_EMAIL='ci@example.invalid', GIT_COMMITTER_EMAIL='ci@example.invalid')
        subprocess.run(['git', '-C', str(self.root), 'commit', '-qm', 'fixture'], env=env, check=True)
        old = self.root / 'go/openai/with tab\tname.go'
        old.touch()
        git('add', '.')
        subprocess.run(['git', '-C', str(self.root), 'commit', '-qm', 'old owner'], env=env, check=True)
        old.rename(self.root / 'go/a2a/with tab\tname.go')
        git('add', '.')
        subprocess.run(['git', '-C', str(self.root), 'commit', '-qm', 'move'], env=env, check=True)
        paths = changed_paths('HEAD~1..HEAD', self.root)
        self.assertIn('go/openai/with tab\tname.go', paths)
        self.assertEqual(self.names(paths), {'openai', 'a2a'})


if __name__ == '__main__':
    unittest.main()
