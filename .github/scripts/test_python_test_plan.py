"""Tests for python_test_plan.py and known_broken_verdict.sh (standard library only).

The Plan job runs these before it plans anything (the job has no pytest):

    python3 -m unittest discover -s .github/scripts -p 'test_*.py' -v

A failure fails Plan, so no package job runs and "Python tests" fails.
"""

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
        ("an unrelated file", ["README.md"], [], False, False),
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


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


class ChangedPaths(FixtureCase):
    ODD_NAME = 'python/frameworks/alpha/src/we"ird\tname.txt'
    OTHER_NAME = "python/frameworks/beta/src/back\\slash é.txt"

    def setUp(self):
        super().setUp()
        # Run git on the fixture repo only: a GIT_DIR or GIT_INDEX_FILE inherited from a git hook
        # would point these commands (and changed_paths) at the outer repo.
        environ = mock.patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        for name in [name for name in os.environ if name.startswith("GIT_")]:
            del os.environ[name]
        write_tree(self.root, {self.ODD_NAME: "odd\n", self.OTHER_NAME: "other\n"})
        git(self.root, "init", "-q")
        git(self.root, "config", "user.name", "Planner Test")
        git(self.root, "config", "user.email", "planner-test@example.com")
        git(self.root, "config", "commit.gpgsign", "false")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")

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
        expected = set(FIXTURE_FILES) | {self.ODD_NAME, self.OTHER_NAME}
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


class CommandLine(unittest.TestCase):
    def test_diff_and_all_are_mutually_exclusive(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as caught:
            planner.main(["--diff", "a..b", "--all"])
        self.assertEqual(caught.exception.code, 2)

    def test_a_bad_range_exits_1(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(planner.main(["--diff", "no-such-ref...HEAD"]), 1)


VERDICT = SCRIPTS_DIR / "known_broken_verdict.sh"


class KnownBrokenVerdict(unittest.TestCase):
    BASE = {
        "PKG_NAME": "pkg",
        "REASON": "the listed reason",
        "INSTALL_POETRY": "success",
        "INSTALL_HATCH": "skipped",
        "CORE_INSTALL": "skipped",
        "TESTS": "failure",
        "OWN_TESTS": "true",
        "CORE_TESTS": "",
    }
    CASES = [
        # (label, env overrides, exit code, a phrase that must be in the output)
        ("tests pass", {"TESTS": "success"}, 1, "remove its line"),
        (
            "tests pass after a failed install",
            {"TESTS": "success", "INSTALL_POETRY": "failure"},
            1,
            "failed in this run: install (poetry)",
        ),
        ("tests fail", {"TESTS": "failure"}, 0, "known broken (the listed reason)"),
        (
            "tests fail after failed poetry and core installs",
            {"TESTS": "failure", "INSTALL_POETRY": "failure", "CORE_INSTALL": "failure"},
            0,
            "install (poetry), fi_instrumentation install",
        ),
        (
            "tests fail after a failed hatch install",
            {"TESTS": "failure", "INSTALL_POETRY": "skipped", "INSTALL_HATCH": "failure"},
            0,
            "install (hatch)",
        ),
        ("tests skipped", {"TESTS": "skipped"}, 1, "did not run"),
        (
            "no tests and the tests pass",
            {"TESTS": "success", "OWN_TESTS": "false", "CORE_TESTS": ""},
            1,
            "has no tests",
        ),
        (
            "core tests only, and they pass",
            {"TESTS": "success", "OWN_TESTS": "false", "CORE_TESTS": "python/tests/test_framework_x.py"},
            1,
            "its tests pass",
        ),
        ("cancelled", {"TESTS": "cancelled"}, 0, "known broken"),
        ("no Tests outcome", {"TESTS": ""}, 1, "unexpected Tests outcome"),
        ("a misspelled outcome", {"TESTS": "succes"}, 1, "unexpected Tests outcome"),
    ]

    def run_verdict(self, overrides):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **self.BASE, **overrides}
        # GitHub runs a bash step as: bash -e {0}; with pipefail it is: bash --noprofile --norc -eo pipefail {0}.
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(VERDICT)],
            env=env,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

    def test_cases(self):
        for label, overrides, code, phrase in self.CASES:
            with self.subTest(label):
                done = self.run_verdict(overrides)
                output = done.stdout + done.stderr
                self.assertEqual(done.returncode, code, output)
                self.assertIn(phrase, output)

    def test_the_syntax_is_valid(self):
        subprocess.run(["bash", "-n", str(VERDICT)], check=True)


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
            self.assertIn(f".github/scripts/{name}", planner.CI_FILES)
            self.assertTrue((REPO_ROOT / ".github/scripts" / name).is_file())


if __name__ == "__main__":
    unittest.main()
