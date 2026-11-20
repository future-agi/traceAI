"""A Go module nested under another module's folder is inventoried, and is not tagged until it can be consumed. Offline."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
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

    def test_this_repository_inventories_the_blank_import_module_with_its_tag(self):
        pkgs = {p["dir"]: p for p in release_plan.other_packages(release_plan.REPO_ROOT) if p["ecosystem"] == "go"}
        self.assertIn("go/eino/auto", pkgs)
        self.assertEqual(pkgs["go/eino/auto"]["name"], NESTED)
        self.assertEqual(pkgs["go/eino/auto"]["tag"], f"go/eino/auto/v{pkgs['go/eino/auto']['version']}")
        self.assertNotIn("go/auto/sdkcompat", pkgs)

    def test_release_config_and_manifest_cover_every_inventoried_module(self):
        root = release_plan.REPO_ROOT
        pkgs = release_plan.python_packages(root) + release_plan.typescript_packages(root) + release_plan.other_packages(root)
        self.assertEqual(release_plan.coverage_problems(root, pkgs), [])
        cfg = json.loads((root / "release-please-config.json").read_text())["packages"]["go/eino/auto"]
        self.assertEqual((cfg["release-type"], cfg["tag-separator"], cfg["initial-version"]), ("go", "/", "0.1.0"))
        self.assertEqual(json.loads((root / ".release-please-manifest.json").read_text())["go/eino/auto"], "0.1.0")


class ReleaseGate(unittest.TestCase):
    def blockers(self, mod, floors=FLOORS):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tree(root, nested_mod=mod, floors=floors)
            pkg = [p for p in release_plan.other_packages(root) if p["dir"] == "go/eino/auto"][0]
            return release_plan.go_release_blockers(root, pkg)

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
            pkgs = [p for p in release_plan.other_packages(root) if p["dir"] == "go/eino/auto"]
            out = release_plan.plan(pkgs, {}, offline=True, root=root)
            self.assertEqual(out["go"], [])
            self.assertEqual([s["name"] for s in out["skipped"]], [NESTED])
            self.assertIn("replace", out["skipped"][0]["reason"])

    def test_this_head_is_refused_until_the_requires_move_past_the_floor(self):
        root = release_plan.REPO_ROOT
        pkg = [p for p in release_plan.other_packages(root) if p["dir"] == "go/eino/auto"][0]
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
