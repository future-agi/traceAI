"""Tests for python_test_plan.py and known_broken_verdict.sh (standard library only).

The Plan job runs these before it plans anything (the job has no pytest):

    python3 -m unittest discover -s .github/scripts -p 'test_*.py' -v

A failure fails Plan, so no package job runs and "Python tests" fails.
"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

import python_test_plan as planner  # noqa: E402

POETRY = '[build-system]\nrequires = ["poetry-core"]\nbuild-backend = "poetry.core.masonry.api"\n'
HATCH = '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'

# Fixture packages: alpha (poetry, own tests), beta (hatchling, listed as known broken),
# gamma (poetry, owns python/tests/test_framework_zeta.py), traceai (poetry).
ALL_PACKAGES = ["alpha", "beta", "gamma", "traceai"]
FIXTURE_FILES = {
    "python/frameworks/alpha/pyproject.toml": POETRY,
    "python/frameworks/alpha/tests/test_x.py": "def test_x():\n    pass\n",
    "python/frameworks/alpha/src/mod.py": "VALUE = 1\n",
    "python/frameworks/beta/pyproject.toml": HATCH,
    "python/frameworks/gamma/pyproject.toml": POETRY,
    "python/traceai/pyproject.toml": POETRY,
    "python/tests/test_framework_zeta.py": "def test_zeta():\n    pass\n",
    "python/tests/conftest.py": "",
    ".github/python-tests-known-broken.txt": "# a comment\n\nbeta | its tests fail today\n",
}
ZETA = {"zeta": "gamma"}


def write_tree(root, files):
    for rel, text in files.items():
        path = Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class FixtureCase(unittest.TestCase):
    """A fixture repo in a temp dir, with test_framework_zeta mapped to the gamma package."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        write_tree(self.root, FIXTURE_FILES)
        # Build a small schema-valid canonical inventory for this synthetic repository.
        data = json.loads((REPO_ROOT / '.github/compat/registry.json').read_text())
        template = next(t for t in data['integrations'] if t['id'] == 'python-google-genai')
        data['integrations'] = []
        import copy
        for name in ALL_PACKAGES:
            directory = 'python/traceai' if name == 'traceai' else f'python/frameworks/{name}'
            manifest = self.root / directory / 'pyproject.toml'
            manifest.write_text(manifest.read_text() + f'\n[project]\nname = "fixture-{name}"\n')
            t = copy.deepcopy(template)
            t.update(id=f'python-{name}', package_dir=directory, distribution=f'fixture-{name}',
                     upstream=[], contract_test=None, notes=[])
            t['declared_range_source'] = dict(path=directory+'/pyproject.toml', key='project.dependencies', kind='metadata')
            t['runtimes'].update(declared=None, representative=[], boundaries=[],
                                source=dict(path=directory+'/pyproject.toml', key='project.requires-python', kind='metadata'))
            data['integrations'].append(t)
        data['defaults']['pilot_ids'] = ['python-alpha', 'python-beta', 'python-gamma']
        write_tree(self.root, {'.github/compat/registry.json': json.dumps(data)})
        patcher = mock.patch.dict(planner.FRAMEWORK_TEST_PACKAGES, ZETA)
        patcher.start()
        self.addCleanup(patcher.stop)

    def names(self, result):
        return [package["name"] for package in result["packages"]]


class PlanTable(FixtureCase):
    CASES = [
        # (label, changed paths, expected package names, run_all, core_changed)
        ("a package file", ["python/frameworks/alpha/src/mod.py"], ["alpha"], False, False),
        ("a package's own test", ["python/frameworks/alpha/tests/test_x.py"], ["alpha"], False, False),
        ("the traceai package", ["python/traceai/traceai/__init__.py"], ["traceai"], False, False),
        ("a core file", ["python/fi_instrumentation/otel.py"], ALL_PACKAGES, False, True),
        ("a nested core file", ["python/fi_instrumentation/instrumentation/context_attributes.py"], ALL_PACKAGES, False, True),
        (
            "CI files and a core file together (core_install runs in every package job)",
            [
                ".github/workflows/python-tests.yml",
                ".github/scripts/known_broken_verdict.sh",
                ".github/scripts/test_python_test_plan.py",
                "python/fi_instrumentation/instrumentation/context_attributes.py",
            ],
            ALL_PACKAGES,
            True,
            True,
        ),
        ("poetry.lock", ["python/poetry.lock"], ALL_PACKAGES, False, True),
        ("python/pyproject.toml", ["python/pyproject.toml"], ALL_PACKAGES, False, True),
        ("conftest.py", ["python/tests/conftest.py"], ["gamma"], False, False),
        ("pytest.ini", ["python/tests/pytest.ini"], ["gamma"], False, False),
        ("a test_framework file", ["python/tests/test_framework_zeta.py"], ["gamma"], False, False),
        ("the workflow", [".github/workflows/python-tests.yml"], ALL_PACKAGES, True, False),
        ("the planner", [".github/scripts/python_test_plan.py"], ALL_PACKAGES, True, False),
        ("the planner's tests", [".github/scripts/test_python_test_plan.py"], ALL_PACKAGES, True, False),
        ("the verdict script", [".github/scripts/known_broken_verdict.sh"], ALL_PACKAGES, True, False),
        ("the known-broken list", [".github/python-tests-known-broken.txt"], ALL_PACKAGES, True, False),
        ("a new helper under .github/scripts", [".github/scripts/new_helper.py"], ALL_PACKAGES, True, False),
        ("a README", ["README.md"], [], False, False),
        ("a docs file", ["docs/x.md"], [], False, False),
        ("a java file", ["java/x"], [], False, False),
        ("a typescript file", ["typescript/x.ts"], [], False, False),
        ("a csharp file", ["csharp/x.cs"], [], False, False),
        ("a python e2e test", ["python/e2e_tests/x.py"], [], False, False),
        ("python/frameworks/tests_e2e", ["python/frameworks/tests_e2e/x.py"], [], False, False),
        ("a core test that is not test_framework", ["python/tests/test_otel.py"], [], False, False),
        ("a package removed entirely", ["python/frameworks/gone/src/x.py"], [], False, False),
        ("a file next to the packages", ["python/frameworks/README.md"], [], False, False),
        ("nothing changed", [], [], False, False),
        (
            "two packages and a core test",
            ["python/frameworks/beta/x.py", "python/tests/test_framework_zeta.py"],
            ["beta", "gamma"],
            False,
            False,
        ),
    ]

    def test_cases(self):
        for label, changed, expected, run_all, core_changed in self.CASES:
            with self.subTest(label):
                result = planner.plan(changed, root=self.root)
                self.assertEqual(self.names(result), expected)
                self.assertIs(result["run_all"], run_all)
                self.assertIs(result["core_changed"], core_changed)

    def test_package_entries(self):
        result = planner.plan(["python/fi_instrumentation/otel.py"], root=self.root)
        by_name = {package["name"]: package for package in result["packages"]}
        self.assertEqual(by_name["alpha"]["backend"], "poetry")
        self.assertIs(by_name["alpha"]["own_tests"], True)
        self.assertEqual(by_name["beta"]["backend"], "hatch")
        self.assertIs(by_name["beta"]["own_tests"], False)
        self.assertIs(by_name["beta"]["known_broken"], True)
        self.assertEqual(by_name["beta"]["known_broken_reason"], "its tests fail today")
        self.assertIs(by_name["alpha"]["known_broken"], False)
        self.assertEqual(by_name["gamma"]["core_tests"], ["python/tests/test_framework_zeta.py"])
        self.assertEqual(by_name["traceai"]["dir"], "python/traceai")


class PlanErrors(FixtureCase):
    def assert_plan_error(self, changed, text):
        with self.assertRaises(planner.PlanError) as caught:
            planner.plan(changed, root=self.root)
        self.assertIn(text, str(caught.exception))

    def set_known_broken(self, text):
        (self.root / planner.KNOWN_BROKEN_FILE).write_text(text, encoding="utf-8")

    def test_framework_test_key_not_in_the_map(self):
        self.assert_plan_error(["python/tests/test_framework_nokey.py"], "not in FRAMEWORK_TEST_PACKAGES")

    def test_framework_test_file_on_disk_with_no_key(self):
        write_tree(self.root, {"python/tests/test_framework_nokey.py": ""})
        self.assert_plan_error(["README.md"], "not in FRAMEWORK_TEST_PACKAGES")

    def test_framework_test_key_maps_to_a_missing_package(self):
        with mock.patch.dict(planner.FRAMEWORK_TEST_PACKAGES, {"zeta": "missing"}):
            self.assert_plan_error([], "not a package directory")

    def test_bad_list_line(self):
        self.set_known_broken("beta without a separator\n")
        self.assert_plan_error([], "expected '<package dir name> | <reason>'")

    def test_empty_reason(self):
        self.set_known_broken("beta |  \n")
        self.assert_plan_error([], "expected '<package dir name> | <reason>'")

    def test_unknown_listed_name(self):
        self.set_known_broken("nosuch | because\n")
        self.assert_plan_error([], "'nosuch' is not a package")

    def test_duplicate_listed_name(self):
        self.set_known_broken("beta | one\nbeta | two\n")
        self.assert_plan_error([], "'beta' is listed twice")

    def test_missing_list(self):
        (self.root / planner.KNOWN_BROKEN_FILE).unlink()
        self.assert_plan_error([], "cannot read the known-broken list")

    def test_unknown_build_backend(self):
        write_tree(self.root, {"python/frameworks/gamma/pyproject.toml": '[build-system]\nbuild-backend = "setuptools.build_meta"\n'})
        self.assert_plan_error([], "unknown build-backend")

    def discover_nothing(self):
        """The fixture with no package at all (as if discovery broke), but otherwise valid."""
        for pyproject in list(self.root.glob("python/**/pyproject.toml")):
            pyproject.unlink()
        (self.root / "python/tests/test_framework_zeta.py").unlink()
        self.set_known_broken("# nothing listed\n")

    def test_run_all_with_0_packages(self):
        self.discover_nothing()
        self.assert_plan_error([".github/workflows/python-tests.yml"], "compatibility registry")

    def test_core_change_with_0_packages(self):
        self.discover_nothing()
        self.assert_plan_error(["python/fi_instrumentation/otel.py"], "compatibility registry")

    def test_a_change_with_0_packages_and_no_run_all_is_not_an_error(self):
        self.discover_nothing()
        self.assert_plan_error(["README.md"], "compatibility registry")

    def test_a_change_in_a_directory_without_a_pyproject(self):
        write_tree(self.root, {"python/frameworks/notpkg/src/mod.py": "VALUE = 1\n"})
        self.assert_plan_error(
            ["python/frameworks/notpkg/src/mod.py"],
            "python/frameworks/notpkg/ changed but has no pyproject.toml; "
            "a package must keep its pyproject.toml (or be removed entirely)",
        )

    def test_a_package_that_lost_its_pyproject(self):
        (self.root / "python/frameworks/alpha/pyproject.toml").unlink()
        self.assert_plan_error(["python/frameworks/alpha/src/mod.py"], "compatibility registry")

    def test_tests_e2e_may_exist_without_a_pyproject(self):
        write_tree(self.root, {"python/frameworks/tests_e2e/x.py": ""})
        result = planner.plan(["python/frameworks/tests_e2e/x.py"], root=self.root)
        self.assertEqual(self.names(result), [])

    def test_a_removed_package_directory_is_fine(self):
        self.assertFalse((self.root / "python/frameworks/gone").exists())
        result = planner.plan(["python/frameworks/gone/pyproject.toml"], root=self.root)
        self.assertEqual(self.names(result), [])


def git(root, *args):
    # No hooks: a core.hooksPath in the user's global config must not run on the fixture's commits.
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


class GitFixtureCase(FixtureCase):
    """The fixture, committed in a git repo of its own (plus EXTRA_FILES)."""

    EXTRA_FILES = {}

    def setUp(self):
        super().setUp()
        # Run git on the fixture repo only: a GIT_DIR or GIT_INDEX_FILE inherited from a git hook
        # would point these commands (and changed_paths) at the outer repo.
        environ = mock.patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        for name in [name for name in os.environ if name.startswith("GIT_")]:
            del os.environ[name]
        write_tree(self.root, self.EXTRA_FILES)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.name", "Planner Test")
        git(self.root, "config", "user.email", "planner-test@example.com")
        git(self.root, "config", "commit.gpgsign", "false")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")


class ChangedPaths(GitFixtureCase):
    ODD_NAME = 'python/frameworks/alpha/src/we"ird\tname.txt'
    OTHER_NAME = "python/frameworks/beta/src/back\\slash é.txt"
    EXTRA_FILES = {ODD_NAME: "odd\n", OTHER_NAME: "other\n"}

    def commit_move(self, source, target):
        (self.root / target).parent.mkdir(parents=True, exist_ok=True)
        git(self.root, "mv", source, target)
        git(self.root, "commit", "-q", "-m", "move")

    def test_a_move_between_packages_lists_both_paths(self):
        old = "python/frameworks/alpha/src/mod.py"
        new = "python/frameworks/beta/src/mod.py"
        self.commit_move(old, new)
        changed = planner.changed_paths("HEAD~1..HEAD", self.root)
        self.assertEqual(sorted(changed), sorted([old, new]))
        result = planner.plan(changed, root=self.root)
        self.assertEqual(self.names(result), ["alpha", "beta"])

    def test_a_name_with_a_tab_and_a_quote_comes_back_exactly(self):
        (self.root / self.ODD_NAME).write_text("changed\n", encoding="utf-8")
        (self.root / self.OTHER_NAME).write_text("changed\n", encoding="utf-8")
        git(self.root, "commit", "-q", "-a", "-m", "edit")
        changed = planner.changed_paths("HEAD~1..HEAD", self.root)
        self.assertEqual(sorted(changed), sorted([self.ODD_NAME, self.OTHER_NAME]))
        self.assertEqual(self.names(planner.plan(changed, root=self.root)), ["alpha", "beta"])

    def test_three_dot_range(self):
        base = git(self.root, "rev-parse", "HEAD").strip()
        (self.root / "README.md").write_text("hi\n", encoding="utf-8")
        git(self.root, "add", "README.md")
        git(self.root, "commit", "-q", "-m", "readme")
        self.assertEqual(planner.changed_paths(f"{base}...HEAD", self.root), ["README.md"])

    def test_none_lists_every_tracked_file(self):
        expected = set(FIXTURE_FILES) | {self.ODD_NAME, self.OTHER_NAME, '.github/compat/registry.json'}
        self.assertEqual(set(planner.changed_paths(None, self.root)), expected)

    def test_no_change_is_an_empty_list(self):
        self.assertEqual(planner.changed_paths("HEAD..HEAD", self.root), [])

    def test_a_git_failure_is_a_plan_error_with_gits_stderr(self):
        with self.assertRaises(planner.PlanError) as caught:
            planner.changed_paths("no-such-ref...HEAD", self.root)
        self.assertIn("no-such-ref", str(caught.exception))

    def test_not_a_git_repository(self):
        with tempfile.TemporaryDirectory() as empty:
            env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            env["GIT_CEILING_DIRECTORIES"] = str(Path(empty).parent)
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(planner.PlanError):
                    planner.changed_paths(None, empty)


class CommandLine(GitFixtureCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = planner.main(argv, root=self.root)
        return code, out.getvalue(), err.getvalue()

    def test_diff_and_all_are_mutually_exclusive(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as caught:
            planner.main(["--diff", "a..b", "--all"], root=self.root)
        self.assertEqual(caught.exception.code, 2)

    def test_a_bad_range_exits_1(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(planner.main(["--diff", "no-such-ref...HEAD"], root=self.root), 1)

    def test_all_plans_every_package(self):
        code, out, _ = self.run_main(["--all"])
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(self.names(result), ALL_PACKAGES)
        # The fixture tracks the known-broken list, which is a CI file; no fi_instrumentation file.
        self.assertIs(result["run_all"], True)
        self.assertIs(result["core_changed"], False)

    def test_all_in_a_repo_with_no_tracked_file_exits_1(self):
        git(self.root, "rm", "-q", "-r", "--cached", ".")
        code, out, err = self.run_main(["--all"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("0 tracked files", err)

    def test_a_range_with_no_commits_exits_1(self):
        # What a PR_BASE_SHA wired to the head SHA gives.
        code, out, err = self.run_main(["--diff", "HEAD...HEAD"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no commits in HEAD...HEAD", err)
        self.assertIn("check the base and head SHAs", err)

    def test_commits_that_change_nothing_plan_nothing(self):
        mod = self.root / "python/frameworks/alpha/src/mod.py"
        for label, make in (
            ("an empty commit", lambda: git(self.root, "commit", "-q", "--allow-empty", "-m", "empty")),
            ("a change and its revert", lambda: (
                mod.write_text("VALUE = 3\n", encoding="utf-8"),
                git(self.root, "commit", "-q", "-a", "-m", "change"),
                git(self.root, "revert", "--no-edit", "HEAD"),
            )),
        ):
            with self.subTest(label):
                base = git(self.root, "rev-parse", "HEAD").strip()
                make()
                for rev_range in (f"{base}...HEAD", f"{base}..HEAD"):
                    code, out, err = self.run_main(["--diff", rev_range])
                    self.assertEqual(code, 0, err)
                    self.assertEqual(json.loads(out)["packages"], [])
                    self.assertIn("no changed files; nothing to plan", err)

    def test_a_diff_with_a_change_plans_it(self):
        (self.root / "python/frameworks/alpha/src/mod.py").write_text("VALUE = 2\n", encoding="utf-8")
        git(self.root, "commit", "-q", "-a", "-m", "edit")
        code, out, _ = self.run_main(["--diff", "HEAD~1...HEAD"])
        self.assertEqual(code, 0)
        self.assertEqual(self.names(json.loads(out)), ["alpha"])


VERDICT = SCRIPTS_DIR / "known_broken_verdict.sh"


class KnownBrokenVerdict(unittest.TestCase):
    def test_wrapper_fails_closed_and_never_uses_package_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            junit = directory / 'suite.xml'
            context = directory / 'context.json'
            context.write_text(json.dumps({'runtime': {'implementation': 'cpython', 'version': '3.11.9'},
                                           'resolved': [{'name': 'google-genai', 'version': '1.8.0'}]}))
            base = {**os.environ, 'PKG_NAME': 'google-genai', 'REASON': 'legacy package-wide reason',
                    'INSTALL_POETRY': 'success', 'INSTALL_HATCH': 'skipped', 'CORE_INSTALL': 'skipped',
                    'TESTS': 'success', 'INTEGRATION_ID': 'python-google-genai',
                    'JUNIT_DIR': tmp, 'TEST_CONTEXT': str(context), 'EXPECTED_SUITES': '1',
                    'TEST_EXIT_CODE': '0', 'VALIDATOR_PYTHON': sys.executable, 'GITHUB_WORKSPACE': str(REPO_ROOT)}
            def run(overrides=None):
                return subprocess.run(['bash', str(VERDICT)], cwd=REPO_ROOT,
                                      env={**base, **(overrides or {})}, capture_output=True, text=True)
            xml = '<testsuites><testsuite><testcase file=".github/compat/contracts/test_google_genai.py" classname="test_google_genai" name="test_plain"/></testsuite></testsuites>'
            junit.write_text(xml)
            self.assertEqual(run().returncode, 0)
            for outcome in ['skipped', 'cancelled', '']:
                self.assertNotEqual(run({'TESTS': outcome}).returncode, 0)
            self.assertNotEqual(run({'INSTALL_POETRY': 'failure'}).returncode, 0)
            self.assertNotEqual(run({'TEST_EXIT_CODE': '2'}).returncode, 0)
            for content in ['<testsuites/>', xml.replace('/></testsuite>', '><skipped/></testcase></testsuite>'),
                            xml.replace('/></testsuite>', '><failure>AssertionError: unrelated</failure></testcase></testsuite>')]:
                junit.write_text(content)
                self.assertNotEqual(run({'TESTS': 'failure', 'TEST_EXIT_CODE': '1'}).returncode, 0)
            junit.unlink()
            self.assertNotEqual(run().returncode, 0)

    def test_the_syntax_is_valid(self):
        subprocess.run(['bash', '-n', str(VERDICT)], check=True)


class WorkflowText(unittest.TestCase):
    """Plain text checks on the workflow file (no YAML parser in the standard library)."""

    @classmethod
    def setUpClass(cls):
        cls.text = (REPO_ROOT / ".github/workflows/python-tests.yml").read_text(encoding="utf-8")

    def test_pull_request_types_include_edited(self):
        match = re.search(r"^  pull_request:\n(?:    .*\n)*?    types: \[([^\]]*)\]", self.text, re.MULTILINE)
        self.assertIsNotNone(match, "no pull_request types list")
        types = [item.strip() for item in match.group(1).split(",")]
        self.assertEqual(types, ["opened", "synchronize", "reopened", "edited"])

    def test_plan_job_calls_the_planner_with_diff_and_all(self):
        self.assertIn('python_test_plan.py --diff "$PR_BASE_SHA...$PR_HEAD_SHA"', self.text)
        self.assertIn('python_test_plan.py --diff "$PUSH_BEFORE_SHA..$PUSH_AFTER_SHA"', self.text)
        self.assertIn("python_test_plan.py --all", self.text)

    def test_plan_job_runs_these_tests_before_planning(self):
        run = "python3 -m unittest discover -s .github/scripts -p 'test_*.py' -v"
        self.assertIn(run, self.text)
        self.assertLess(self.text.index(run), self.text.index("Work out which packages this change touches"))

    def test_the_verdict_step_runs_the_script(self):
        self.assertIn("run: bash .github/scripts/known_broken_verdict.sh", self.text)

    def test_the_verdict_step_reads_the_outcomes_of_steps_that_exist(self):
        # A renamed step id leaves its outcome empty; the script fails on that, and this names it.
        for var, step in (
            ("INSTALL_POETRY", "install_poetry"),
            ("INSTALL_HATCH", "install_hatch"),
            ("CORE_INSTALL", "core_install"),
            ("TESTS", "tests"),
        ):
            with self.subTest(var):
                self.assertIn(f"{var}: ${{{{ steps.{step}.outcome }}}}", self.text)
                self.assertRegex(self.text, rf"(?m)^        id: {step}$")

    def test_ci_files_cover_the_new_files(self):
        for name in ("test_python_test_plan.py", "known_broken_verdict.sh", "python_test_plan.py"):
            self.assertTrue(planner.is_ci_path(f".github/scripts/{name}"))
            self.assertTrue((REPO_ROOT / ".github/scripts" / name).is_file())
        self.assertTrue(planner.is_ci_path(".github/scripts/a_helper_added_later.py"))
        self.assertTrue(planner.is_ci_path(".github/workflows/python-tests.yml"))
        self.assertTrue(planner.is_ci_path(planner.KNOWN_BROKEN_FILE))
        self.assertFalse(planner.is_ci_path(".github/CODEOWNERS"))
        self.assertFalse(planner.is_ci_path(".github/scriptsX/a.py"))

    @classmethod
    def lines(cls):
        return [line.strip() for line in cls.text.splitlines()]

    @classmethod
    def job(cls, key):
        """The stripped lines of the job `  <key>:`, up to the next job."""
        lines = cls.text.splitlines()
        start = lines.index(f"  {key}:")
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"^  \S|^\S", lines[i])), len(lines))
        return [item.strip() for item in lines[start:end]]

    def test_plan_outputs_reach_the_package_job(self):
        plan = self.job("plan")
        for line in (
            "matrix: ${{ steps.plan.outputs.matrix }}",
            "has_packages: ${{ steps.plan.outputs.has_packages }}",
            "core_changed: ${{ steps.plan.outputs.core_changed }}",
        ):
            self.assertIn(line, plan)
        # A planner error must fail Plan, never be swallowed.
        self.assertFalse([line for line in plan if line.startswith("continue-on-error")], plan)
        package = self.job("package")
        self.assertIn("if: needs.plan.outputs.has_packages == 'true'", package)
        self.assertIn("package: ${{ fromJSON(needs.plan.outputs.matrix) }}", package)
        self.assertIn("if: needs.plan.outputs.core_changed == 'true'", package)

    def test_the_result_job_judges_the_package_jobs_by_the_planned_matrix(self):
        result = self.job("result")
        for line in (
            "name: Python tests",
            "needs: [plan, fi-instrumentation, package]",
            "if: always()",
            "PACKAGE: ${{ needs.package.result }}",
            "MATRIX: ${{ needs.plan.outputs.matrix }}",
            'if [ -z "$MATRIX" ]; then',
            'elif [ "$MATRIX" = "[]" ]; then',
            '[ "$PACKAGE" = "skipped" ] || { echo "::error::no packages were planned, but the package jobs ran ($PACKAGE)"; exit 1; }',
            'elif [ "$PACKAGE" != "success" ]; then',
        ):
            self.assertIn(line, result)
        self.assertEqual(sum(line == "exit 1" for line in result), 2, result)

    def test_the_tests_step_runs_every_planned_set_and_reports_failures(self):
        tests = self.step("Tests", containing="id: tests")
        for line in (
            "continue-on-error: true",
            'if [ "$OWN_TESTS" = "true" ]; then',
            "for test_file in $CORE_TESTS; do",
            '[ "$OWN_TESTS" = "true" ] && expected=$((expected + 1))',
            "expected=$((expected + $(echo $CORE_TESTS | wc -w)))",
            'if [ "$ran" != "$expected" ]; then',
            'exit "$status"',
        ):
            self.assertIn(line, tests)
        self.assertEqual(sum("status=$((rc > status ? rc : status))" in line for line in tests), 3, tests)
        self.assertEqual(sum(line == "ran=$((ran + 1))" for line in tests), 3, tests)

    @classmethod
    def step(cls, name, containing=None):
        """The lines of the step called `name` (`      - name: ...`), up to the next step.

        Two steps can share a name (the Tests step of two jobs): `containing` picks the one with that line.
        """
        lines = cls.text.splitlines()
        found = []
        for start, line in enumerate(lines):
            if line == f"      - name: {name}":
                end = next((i for i in range(start + 1, len(lines)) if re.match(r"^      - |^  \S|^\S", lines[i])), len(lines))
                found.append([item.strip() for item in lines[start:end]])
        found = [block for block in found if containing is None or containing in block]
        assert len(found) == 1, f"{len(found)} steps named {name!r} containing {containing!r}"
        return found[0]

    def test_the_plan_step_gets_the_shas_from_the_event(self):
        # A wrong event field (head.sha as the base, say) gives an empty diff and a plan of nothing.
        lines = self.lines()
        for line in (
            "PR_BASE_SHA: ${{ github.event.pull_request.base.sha }}",
            "PR_HEAD_SHA: ${{ github.event.pull_request.head.sha }}",
            "PUSH_BEFORE_SHA: ${{ github.event.before }}",
            "PUSH_AFTER_SHA: ${{ github.event.after }}",
        ):
            with self.subTest(line):
                self.assertEqual(lines.count(line), 1)

    def test_the_planner_tests_step_cannot_be_skipped_or_ignored(self):
        step = self.step("Test the planner and the known-broken verdict")
        self.assertIn("run: python3 -m unittest discover -s .github/scripts -p 'test_*.py' -v", step)
        for line in step:
            self.assertFalse(line.startswith(("continue-on-error", "if:")), line)

    def test_every_run_step_uses_bash_with_pipefail(self):
        # `shell: bash` under the workflow's `defaults: run:` makes GitHub run every run step as
        # `bash --noprofile --norc -eo pipefail {0}` (a bare run step gets `bash -e {0}`, with no pipefail,
        # so `cmd | tee log` would hide a failed cmd). The block is exactly that, and the file has
        # exactly one `shell:` anywhere (flow style and comments included), so no job or step overrides it.
        lines = self.text.splitlines()
        self.assertEqual(lines.count("defaults:"), 1)
        start = lines.index("defaults:")
        self.assertEqual(lines[start : start + 3], ["defaults:", "  run:", "    shell: bash"])
        self.assertFalse(lines[start + 3].startswith(" "), "another key under defaults")
        self.assertEqual(len(re.findall(r"\bshell[\"']?\s*:", self.text)), 1)

    INSTALL_STEPS = (
        # (step id, step name, its if:, the install command that must stand alone on a line)
        (
            "install_poetry",
            "Install the package and its dev dependencies (poetry)",
            "if: matrix.package.backend == 'poetry'",
            "poetry install --no-interaction",
        ),
        (
            "install_hatch",
            "Install the package and its dev dependencies (hatch)",
            "if: matrix.package.backend == 'hatch'",
            '.venv/bin/pip install -e ".[dev]"',
        ),
        (
            "core_install",
            "Test against this repo's fi_instrumentation",
            "if: needs.plan.outputs.core_changed == 'true'",
            '"$GITHUB_WORKSPACE/$PKG_DIR/.venv/bin/pip" install -e "$GITHUB_WORKSPACE/python"',
        ),
    )

    # The commands of each install step, exactly as reviewed (comments and indentation aside). A line
    # filter cannot see a multi-line wrapper that exempts the install from errexit (`{ ...; } && ...`,
    # a function called from `if`, `trap ... ERR`, `bash -c '...'`), so the whole script is pinned:
    # changing an install step means changing this table, in the same diff.
    INSTALL_SCRIPTS = {
        "install_poetry": [
            'pip install "poetry==$POETRY_VERSION"',
            "poetry install --no-interaction",
            "for pin in pytest==8.4.2 pytest-asyncio==0.24.0 pytest-mock==3.14.0; do",
            'module=$(echo "${pin%%==*}" | tr - _)',
            'if poetry run python -c "import $module" 2>/dev/null; then',
            'echo "$module: already present"',
            "else",
            'echo "$module: added $pin"',
            'poetry run pip install "$pin"',
            "fi",
            "done",
        ],
        "install_hatch": [
            "python -m venv .venv",
            '.venv/bin/pip install -e ".[dev]"',
            "for pin in pytest==8.4.2 pytest-asyncio==0.24.0 pytest-mock==3.14.0; do",
            'module=$(echo "${pin%%==*}" | tr - _)',
            'if .venv/bin/python -c "import $module" 2>/dev/null; then',
            'echo "$module: already present"',
            "else",
            'echo "$module: added $pin"',
            '.venv/bin/pip install "$pin"',
            "fi",
            "done",
        ],
        "core_install": [
            '"$GITHUB_WORKSPACE/$PKG_DIR/.venv/bin/pip" install -e "$GITHUB_WORKSPACE/python"',
        ],
    }

    def test_the_install_scripts_are_exactly_as_reviewed(self):
        for step_id, name, _condition, _command in self.INSTALL_STEPS:
            with self.subTest(step_id):
                step = self.step(name, containing=f"id: {step_id}")
                commands = [line for line in self.run_lines(step) if line and not line.startswith("#")]
                self.assertEqual(commands, self.INSTALL_SCRIPTS[step_id])

    @staticmethod
    def run_lines(step):
        """The lines of the step's `run: |` block (the run block is the last key of the install steps)."""
        index = step.index("run: |")
        return step[index + 1 :]

    def test_a_failed_install_fails_its_step_for_a_listed_package_too(self):
        # With continue-on-error on an install step, a failed install of a listed package would not
        # fail the job, and the Tests step would run on a broken install. So, for each install step:
        #   - no continue-on-error, no `shell:` and exactly the pinned `if:`;
        #   - over its run lines (comments included): no `||`, no `|` outside a `$( ... )` span (so no
        #     `cmd | tee log`; a `$(...)` is stripped before looking, nested ones are not handled), and
        #     no `set +` (so no `set +e` and no `set +o errexit`);
        #   - the install command is a line of its own, exactly: an `if ! ...; then` wrapper, a trailing
        #     comment or a pipe on that line is not the exact line, so it fails.
        for step_id, name, condition, command in self.INSTALL_STEPS:
            with self.subTest(step_id):
                step = self.step(name, containing=f"id: {step_id}")
                self.assertEqual([line for line in step if line.startswith("continue-on-error")], [], step)
                self.assertEqual([line for line in step if re.search(r"\bshell[\"']?\s*:", line)], [], step)
                self.assertEqual([line for line in step if line.startswith("if:")], [condition], step)
                run = self.run_lines(step)
                self.assertIn(command, run)
                self.assertEqual(run.count(command), 1, run)
                self.assertEqual([line for line in run if "||" in line], [], run)
                outside = [re.sub(r"\$\([^)]*\)", "", line) for line in run]
                self.assertEqual([line for line in outside if "|" in line], [], run)
                self.assertEqual([line for line in run if "set +" in line], [], run)
        # Only the Tests step may be allowed to fail (for a listed package).
        tests = self.step("Tests", containing="id: tests")
        self.assertEqual(tests.count("continue-on-error: true"), 1)
        self.assertEqual(sum(line.startswith("continue-on-error") for line in self.lines()), 1)

    def test_the_tests_step_is_skipped_after_a_failed_install(self):
        # The Tests step (id: tests) has no `if:`, so GitHub gives it the default success(): a failed
        # install step skips it, and a broken install never gets its tests run.
        tests = self.step("Tests", containing="id: tests")
        self.assertEqual([line for line in tests if line.startswith("if:")], [], tests)

    def test_every_job_has_a_time_limit(self):
        # Every job under `jobs:` has exactly one job-level `timeout-minutes: N` (4 spaces, N a positive
        # integer; a step's own timeout does not bound the job) and an entry in this table, so a new job
        # must get both.
        limits = {"plan": 10, "fi-instrumentation": 30, "package": 45, "result": 5}
        lines = self.text.splitlines()

        def is_comment(line):
            return line.lstrip().startswith("#")

        # A comment line (at any indent) neither ends the jobs block nor a job, and a job key may carry
        # a trailing comment.
        start = lines.index("jobs:")
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\S", lines[i]) and not is_comment(lines[i])), len(lines))
        found_keys = {}
        for i in range(start + 1, end):
            match = re.match(r"^  ([A-Za-z0-9_-]+):\s*(#.*)?$", lines[i])
            if match:
                found_keys[match.group(1)] = i
        keys = list(found_keys)
        self.assertEqual(sorted(keys), sorted(limits), "a job without an entry in the table, or an entry without a job")
        for key in keys:
            with self.subTest(key):
                first = found_keys[key]
                last = next(
                    (i for i in range(first + 1, len(lines)) if re.match(r"^  \S|^\S", lines[i]) and not is_comment(lines[i])),
                    len(lines),
                )
                found = [line for line in lines[first:last] if re.match(r"^    timeout-minutes:", line)]
                self.assertEqual(len(found), 1, found)
                self.assertRegex(found[0], r"^    timeout-minutes: [1-9][0-9]*$")
                self.assertEqual(found, [f"    timeout-minutes: {limits[key]}"])

    def test_the_verdict_step_runs_always_for_known_broken_packages(self):
        step = self.step("Known-broken verdict")
        self.assertIn("if: always()", step)

    def test_has_packages_is_read_from_the_plan(self):
        expression = """has_packages=$(jq '.packages | length > 0' "$RUNNER_TEMP/plan.json")"""
        self.assertIn(f'echo "{expression}"', self.lines())

    def test_core_tests_reach_the_tests_and_verdict_steps(self):
        line = "CORE_TESTS: ${{ join(matrix.package.core_tests, ' ') }}"
        self.assertEqual(self.lines().count(line), 2)
        self.assertIn(line, self.step("Tests", containing="id: tests"))
        self.assertIn(line, self.step("Known-broken verdict"))

    def test_a_pr_base_missing_from_the_clone_is_fetched_then_everything_runs(self):
        text = self.text
        check = 'if ! git cat-file -e "$PR_BASE_SHA^{commit}" 2>/dev/null; then'
        fetch = 'timeout 120 git fetch --no-tags origin "$PR_BASE_SHA" || true'
        message = 'echo "PR base $PR_BASE_SHA is not in the clone: treating every file as changed" >&2'
        diff = 'python3 .github/scripts/python_test_plan.py --diff "$PR_BASE_SHA...$PR_HEAD_SHA"'
        lines = self.lines()
        self.assertEqual(lines.count(check), 2)
        self.assertEqual(lines.count(fetch), 1)
        self.assertEqual(lines.count(message), 1)
        self.assertLess(text.index(fetch), text.index(message))
        self.assertLess(text.index(message), text.index(diff))
        # plan_all follows the message, inside the pull_request branch.
        self.assertEqual(lines[lines.index(message) + 1], "plan_all")


if __name__ == "__main__":
    unittest.main()
