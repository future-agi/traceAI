"""A Go module nested under another module's folder is inventoried, and is not tagged until it can be consumed. Offline."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import complete_release
import release_plan

AUTO = "github.com/future-agi/traceAI/go/auto"
EINO = "github.com/future-agi/traceAI/go/eino"
NESTED = "github.com/future-agi/traceAI/go/eino/auto"
SCRIPT = Path(__file__).parent / "release_plan.py"


def tree(root: Path, *, nested_mod: str, config_paths=("go/eino", "go/eino/auto"), floors=None) -> None:
    for d, mod in (("go/eino", "module " + EINO + "\n\ngo 1.26.0\n"), ("go/eino/auto", nested_mod),
                   ("go/auto/sdkcompat", "module " + AUTO + "/sdkcompat\n\ngo 1.26.0\n")):
        (root / d).mkdir(parents=True, exist_ok=True)
        (root / d / "go.mod").write_text(mod)
    (root / "release-please-config.json").write_text(json.dumps({"packages": {p: {"release-type": "go"} for p in config_paths}}))
    (root / ".release-please-manifest.json").write_text(json.dumps({p: "0.1.0" for p in config_paths}))
    if floors is not None:
        (root / ".github/release").mkdir(parents=True, exist_ok=True)
        (root / ".github/release/go_module_floors.json").write_text(json.dumps(floors))


def go_mod(*, auto="v0.2.1", eino="v0.1.1", replace=False) -> str:
    text = f"module {NESTED}\n\ngo 1.26.0\n\nrequire (\n\t{AUTO} {auto}\n\t{EINO} {eino}\n)\n"
    if replace:
        text += f"\nreplace {AUTO} => ../../auto\n"
    return text


FLOORS = {"go/eino/auto": {AUTO: "0.2.0", EINO: "0.1.0"}}


class NestedGoModules(unittest.TestCase):
    def test_a_nested_module_named_in_the_release_config_is_inventoried(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tree(root, nested_mod=go_mod())
            dirs = {p["dir"] for p in release_plan.other_packages(root) if p["ecosystem"] == "go"}
            self.assertEqual(dirs, {"go/eino", "go/eino/auto"})  # not go/auto/sdkcompat: unpublished test module

    def test_the_blank_import_module_stays_out_of_the_release_config_until_it_is_releasable(self):
        root = release_plan.REPO_ROOT
        cfg = json.loads((root / "release-please-config.json").read_text())["packages"]
        manifest = json.loads((root / ".release-please-manifest.json").read_text())
        self.assertNotIn("go/eino/auto", cfg)
        self.assertNotIn("go/eino/auto", manifest)
        # Its files are excluded from go/eino's own release, so go/eino's version and changelog
        # do not move with it.
        self.assertIn("go/eino/auto", cfg["go/eino"].get("exclude-paths", []))
        pkgs = {p["dir"] for p in release_plan.other_packages(root) if p["ecosystem"] == "go"}
        self.assertNotIn("go/eino/auto", pkgs)
        self.assertNotIn("go/auto/sdkcompat", pkgs)

    def test_checks_pass_with_the_module_pending(self):
        root = release_plan.REPO_ROOT
        pkgs = release_plan.python_packages(root) + release_plan.typescript_packages(root) + release_plan.other_packages(root)
        self.assertEqual(release_plan.coverage_problems(root, pkgs), [])
        self.assertEqual(release_plan.main(["--check"]), 0)

    def test_every_go_module_in_the_release_manifest_can_be_tagged_by_the_plan(self):
        # A module the manifest carries but the plan refuses to tag leaves its merged release PR
        # pending, and release-please opens no further release PR while one is pending.
        root = release_plan.REPO_ROOT
        manifest = json.loads((root / ".release-please-manifest.json").read_text())
        go = [p for p in release_plan.other_packages(root) if p["ecosystem"] == "go" and p["dir"] in manifest]
        out = release_plan.plan(go, {}, offline=True)
        self.assertEqual(out["skipped"], [])
        self.assertEqual({p["dir"] for p in out["go"]}, {p["dir"] for p in go})

    def test_a_merged_release_pr_for_the_current_tree_is_completed(self):
        root = release_plan.REPO_ROOT
        manifest = json.loads((root / ".release-please-manifest.json").read_text())
        packages = release_plan.python_packages(root) + release_plan.typescript_packages(root) + release_plan.other_packages(root)
        go = {p["dir"]: p for p in packages if p["ecosystem"] == "go"}
        changed = {path: manifest[path] for path in go if path in manifest}
        self.assertNotIn("go/eino/auto", changed)
        tagged = {p["tag"] for p in release_plan.plan(list(go.values()), {}, offline=True)["go"]}
        pending = [{"number": 7, "headRefName": "release-please--branches--main", "mergeCommit": {"oid": "m"}}]
        gh = [pending, [[{"tag_name": t} for t in sorted(tagged)]]]
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/repo"}), \
                patch.object(complete_release, "gh_json", side_effect=gh), \
                patch.object(complete_release, "changed_release_paths", return_value=changed), \
                patch.object(complete_release, "python_packages", return_value=[]), \
                patch.object(complete_release, "typescript_packages", return_value=[]), \
                patch.object(complete_release, "other_packages", return_value=list(go.values())), \
                patch.object(complete_release, "load_config", return_value={"skip": {}}), \
                patch.object(complete_release, "published_versions", side_effect=lambda p, **kw: {p["version"]}), \
                patch("subprocess.run") as run:
            complete_release.main()
        edits = [c for c in run.call_args_list if "edit" in c.args[0]]
        self.assertEqual(len(edits), 1, "the release PR stays pending")


class PendingGoModules(unittest.TestCase):
    """A module listed in go_module_floors.json is known to be not yet releasable."""

    def problems(self, mod, *, in_config=False, floors=FLOORS, make=True):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tree(root, nested_mod=mod, config_paths=("go/eino", "go/eino/auto") if in_config else ("go/eino",), floors=floors)
            if not make:
                (root / "go/eino/auto/go.mod").unlink()
            return release_plan.pending_go_problems(root)

    def test_a_pending_module_with_replaces_is_accepted(self):
        self.assertEqual(self.problems(go_mod(replace=True, auto="v0.2.0", eino="v0.1.0")), [])

    def test_a_pending_module_in_the_release_config_is_a_problem(self):
        found = self.problems(go_mod(replace=True), in_config=True)
        self.assertTrue(any("release config" in p for p in found), found)

    def test_a_module_that_became_releasable_must_leave_the_floors_file_and_enter_the_config(self):
        found = self.problems(go_mod())
        self.assertTrue(any("releasable" in p for p in found), found)

    def test_a_floors_entry_without_a_module_is_a_problem(self):
        self.assertTrue(self.problems(go_mod(), make=False))

    def test_a_module_in_the_release_config_that_the_gate_refuses_is_a_problem(self):
        # Nothing excuses another module: if it is in the release config, it must be taggable.
        found = self.problems(go_mod(replace=True), in_config=True, floors={})
        self.assertTrue(any("go/eino/auto" in p and "release config" in p for p in found), found)


class ReleaseGate(unittest.TestCase):
    def blockers(self, mod, floors=FLOORS):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tree(root, nested_mod=mod, floors=floors)
            return release_plan.go_release_blockers(root, {"dir": "go/eino/auto", "name": NESTED})

    def test_a_local_replace_is_refused(self):
        self.assertTrue(any("replace" in b for b in self.blockers(go_mod(replace=True))))

    def test_requires_that_are_not_newer_than_the_floor_are_refused(self):
        found = self.blockers(go_mod(auto="v0.2.0", eino="v0.1.0"))
        self.assertEqual(len([b for b in found if "requires" in b]), 2, found)

    def test_released_requires_without_a_replace_pass(self):
        self.assertEqual(self.blockers(go_mod()), [])

    def test_a_module_without_a_floor_entry_has_no_requirement_beyond_the_replace_rule(self):
        self.assertEqual(self.blockers(go_mod(auto="v0.2.0"), floors={}), [])

    def test_the_plan_does_not_tag_a_refused_module(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tree(root, nested_mod=go_mod(replace=True), floors=FLOORS)
            pkgs = [{"ecosystem": "go", "name": NESTED, "version": "0.1.0", "dir": "go/eino/auto", "tag": "go/eino/auto/v0.1.0", "core": False}]
            out = release_plan.plan(pkgs, {}, offline=True, root=root)
            self.assertEqual(out["go"], [])
            self.assertEqual([s["name"] for s in out["skipped"]], [NESTED])
            self.assertIn("replace", out["skipped"][0]["reason"])

    def test_this_head_is_refused_until_the_requires_move_past_the_floor(self):
        root = release_plan.REPO_ROOT
        pkg = {"ecosystem": "go", "name": NESTED, "version": "0.1.0", "dir": "go/eino/auto", "tag": "go/eino/auto/v0.1.0", "core": False}
        found = release_plan.go_release_blockers(root, pkg)
        self.assertTrue(found, "the development go.mod must not be taggable")
        out = release_plan.plan([pkg], {}, offline=True)
        self.assertEqual(out["go"], [])

    def test_the_command_line_gate_exits_non_zero_for_a_refused_module(self):
        res = subprocess.run([sys.executable, str(SCRIPT), "--go-gate", "go/eino/auto"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("go/eino/auto", res.stderr)

    def test_the_go_release_workflow_runs_the_gate_before_it_tags(self):
        text = (Path(__file__).parents[1] / "workflows/release-go.yml").read_text()
        self.assertIn("--go-gate", text)
        self.assertLess(text.index("--go-gate"), text.index("github_release.py"))


if __name__ == "__main__":
    unittest.main()
