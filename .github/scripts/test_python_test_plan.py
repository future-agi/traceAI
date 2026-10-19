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
        self.assert_plan_error([".github/workflows/python-tests.yml"], "run_all is set but 0 packages were found")

    def test_core_change_with_0_packages(self):
        self.discover_nothing()
        self.assert_plan_error(["python/fi_instrumentation/otel.py"], "core_changed is set but 0 packages were found")

    def test_a_change_with_0_packages_and_no_run_all_is_not_an_error(self):
        self.discover_nothing()
        self.assertEqual(planner.plan(["README.md"], root=self.root)["packages"], [])

    def test_a_change_in_a_directory_without_a_pyproject(self):
        write_tree(self.root, {"python/frameworks/notpkg/src/mod.py": "VALUE = 1\n"})
        self.assert_plan_error(
            ["python/frameworks/notpkg/src/mod.py"],
            "python/frameworks/notpkg/ changed but has no pyproject.toml; "
            "a package must keep its pyproject.toml (or be removed entirely)",
        )

    def test_a_package_that_lost_its_pyproject(self):
        (self.root / "python/frameworks/alpha/pyproject.toml").unlink()
        self.assert_plan_error(["python/frameworks/alpha/src/mod.py"], "python/frameworks/alpha/ changed but has no pyproject.toml")

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


INSTALL_FAILS_THE_CHECK = "a failed install fails the check, listed or not"


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
            f"pkg: install (poetry) failed; {INSTALL_FAILS_THE_CHECK}",
        ),
        ("tests fail", {"TESTS": "failure"}, 0, "known broken (the listed reason)"),
        (
            "tests fail after failed poetry and core installs",
            {"TESTS": "failure", "INSTALL_POETRY": "failure", "CORE_INSTALL": "failure"},
            1,
            f"pkg: install (poetry), fi_instrumentation install failed; {INSTALL_FAILS_THE_CHECK}",
        ),
        (
            "tests fail after a failed hatch install",
            {"TESTS": "failure", "INSTALL_POETRY": "skipped", "INSTALL_HATCH": "failure"},
            1,
            f"pkg: install (hatch) failed; {INSTALL_FAILS_THE_CHECK}",
        ),
        (
            "tests fail after a failed core install",
            {"TESTS": "failure", "CORE_INSTALL": "failure"},
            1,
            f"pkg: fi_instrumentation install failed; {INSTALL_FAILS_THE_CHECK}",
        ),
        (
            "tests skipped after a failed install (the real case: a failed install skips the Tests step)",
            {"TESTS": "skipped", "INSTALL_POETRY": "failure"},
            1,
            f"pkg: install (poetry) failed; {INSTALL_FAILS_THE_CHECK}",
        ),
        (
            "cancelled after a failed install",
            {"TESTS": "cancelled", "INSTALL_HATCH": "failure"},
            1,
            f"pkg: install (hatch) failed; {INSTALL_FAILS_THE_CHECK}",
        ),
        (
            "no tests, the tests pass, and a failed install",
            {"TESTS": "success", "OWN_TESTS": "false", "CORE_TESTS": "", "CORE_INSTALL": "failure"},
            1,
            INSTALL_FAILS_THE_CHECK,
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
        ("tests fail, no install failed (skipped installs)", {"TESTS": "failure", "INSTALL_POETRY": "skipped", "CORE_INSTALL": "skipped"}, 0, "known broken (the listed reason)"),
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
                # The old "may not reflect the listed reason" warning and "(failed in this run: ...)" note are gone.
                self.assertNotIn("may not reflect", output)
                self.assertNotIn("failed in this run", output)

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
            "continue-on-error: ${{ matrix.package.known_broken }}",
            'if [ "$OWN_TESTS" = "true" ]; then',
            "for test_file in $CORE_TESTS; do",
            '[ "$OWN_TESTS" = "true" ] && expected=$((expected + 1))',
            "expected=$((expected + $(echo $CORE_TESTS | wc -w)))",
            'if [ "$ran" != "$expected" ]; then',
            'exit "$status"',
        ):
            self.assertIn(line, tests)
        self.assertEqual(sum(line.endswith("|| status=1") for line in tests), 2, tests)
        self.assertEqual(sum(line == "ran=$((ran + 1))" for line in tests), 2, tests)

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

    def test_a_failed_install_fails_its_step_for_a_listed_package_too(self):
        # With continue-on-error on an install step, a failed install of a listed package would not
        # fail the job, and the Tests step would run on a broken install.
        for step_id, name in (
            ("install_poetry", "Install the package and its dev dependencies (poetry)"),
            ("install_hatch", "Install the package and its dev dependencies (hatch)"),
            ("core_install", "Test against this repo's fi_instrumentation"),
        ):
            with self.subTest(step_id):
                step = self.step(name, containing=f"id: {step_id}")
                self.assertEqual([line for line in step if line.startswith("continue-on-error")], [], step)
                # Nor may a command in it swallow its own failure.
                self.assertEqual([line for line in step if line.endswith("|| true") or "set +e" in line], [], step)
        # Only the Tests step may be allowed to fail (for a listed package).
        tests = self.step("Tests", containing="id: tests")
        self.assertEqual(tests.count("continue-on-error: ${{ matrix.package.known_broken }}"), 1)
        self.assertEqual(sum(line.startswith("continue-on-error") for line in self.lines()), 1)

    def test_every_job_has_a_time_limit(self):
        for key, minutes in (("plan", 10), ("fi-instrumentation", 30), ("package", 45), ("result", 5)):
            with self.subTest(key):
                # At the job's own level (4 spaces): a step's timeout-minutes does not bound the job.
                lines = self.text.splitlines()
                start = lines.index(f"  {key}:")
                end = next((i for i in range(start + 1, len(lines)) if re.match(r"^  \S|^\S", lines[i])), len(lines))
                found = [line for line in lines[start:end] if re.match(r"^\s*timeout-minutes:", line)]
                self.assertEqual(found, [f"    timeout-minutes: {minutes}"])
        self.assertEqual(sum(line.startswith("timeout-minutes:") for line in self.lines()), 4)

    def test_the_verdict_step_runs_always_for_known_broken_packages(self):
        step = self.step("Known-broken verdict")
        self.assertIn("if: always() && matrix.package.known_broken", step)

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
