"""Regression coverage for the one-time unpublished-fix audit (no network)."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import bootstrap_audit


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.package = {"ecosystem": "python", "name": "traceAI-openai", "version": "0.1.10",
                        "manifest": "python/frameworks/openai/pyproject.toml", "dir": "python/frameworks/openai",
                        "release_path": "python/frameworks/openai"}
        self.registry = {"releases": {"0.1.10": [
            {"upload_time_iso_8601": "2026-03-10T13:54:40Z"},
            {"upload_time_iso_8601": "2026-03-10T13:54:41Z"}]}}

    def test_python_uses_last_distribution_upload_not_first(self):
        self.assertEqual(bootstrap_audit.upload_time(self.package, self.registry), "2026-03-10T13:54:41Z")
        self.assertIsNone(bootstrap_audit.upload_time(self.package, {"releases": {"0.1.10": []}}))

    def test_npm_requires_version_and_exact_version_time(self):
        p = {**self.package, "ecosystem": "typescript"}
        self.assertEqual(bootstrap_audit.upload_time(p, {"versions": {"0.1.10": {}}, "time": {"0.1.10": "date"}}), "date")
        self.assertIsNone(bootstrap_audit.upload_time(p, {"versions": {}, "time": {"modified": "date"}}))

    def test_patch_target_is_not_already_uploaded(self):
        self.assertEqual(bootstrap_audit.patch_version("0.1.10", {"0.1.10", "0.1.11", "0.1.12", "0.2.0"}), "0.1.13")

    def test_registry_absence_does_not_trigger_unnecessary_bump(self):
        with patch.object(bootstrap_audit, "_get_json", return_value=None), patch.object(bootstrap_audit, "package_history") as history:
            p = bootstrap_audit.audit_package(Path("/tmp"), "baseline", self.package, {})
        self.assertEqual(p["status"], "unpublished_current_version")
        history.assert_not_called()

    def test_unchanged_published_version_is_not_bumped(self):
        with patch.object(bootstrap_audit, "_get_json", return_value=self.registry), patch.object(bootstrap_audit, "package_history", return_value=[]):
            p = bootstrap_audit.audit_package(Path("/tmp"), "baseline", self.package, {})
        self.assertEqual(p["status"], "unchanged_since_upload")

    def test_openai_style_merged_fix_requires_patch(self):
        with patch.object(bootstrap_audit, "_get_json", return_value=self.registry), patch.object(bootstrap_audit, "package_history", return_value=[{"sha": "fix"}]):
            p = bootstrap_audit.audit_package(Path("/tmp"), "baseline", self.package, {})
        self.assertEqual((p["status"], p["next_version"]), ("patch_required", "0.1.11"))

    def test_history_uses_merge_dates_and_excludes_sibling_sdk_for_core(self):
        with patch.object(bootstrap_audit, "git", return_value="sha\t2026-10-28T00:00:00Z\tMerge fix\n") as git:
            bootstrap_audit.package_history(Path("/tmp"), "baseline", {"dir": "python"}, "date")
        args = git.call_args.args
        self.assertIn("--first-parent", args)
        self.assertIn("--since-as-filter=date", args)
        self.assertIn(":(exclude)python/frameworks", args)

    def test_one_time_floor_allows_later_releases_but_catches_stranded_versions(self):
        p = {**self.package, "status": "patch_required", "next_version": "0.1.11"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / p["manifest"]
            file.parent.mkdir(parents=True)
            for version, passes in (("0.1.10", False), ("0.1.11", True), ("0.2.0", True)):
                file.write_text(f'[project]\nversion="{version}"\n')
                (root / ".release-please-manifest.json").write_text(json.dumps({p["release_path"]: version}))
                self.assertEqual(bootstrap_audit.check(root, {"packages": [p]}) == [], passes)

    def test_manifest_inventory_reads_ref_without_importing_package_code(self):
        files = "python/pyproject.toml\ntypescript/packages/a/package.json\ntypescript/packages/private/package.json\nREADME.md\n"
        with patch.object(bootstrap_audit, "git", side_effect=[files, '[project]\nname="core"\nversion="1.0.0"', '{"name":"a","version":"0.1.0"}', '{"name":"private","version":"1.0.0","private":true}']):
            packages = bootstrap_audit.manifests(Path("/tmp"), "baseline")
        self.assertEqual([p["name"] for p in packages], ["core", "a"])
        self.assertEqual(packages[0]["release_path"], "python/fi_instrumentation")

    def test_committed_audit_covers_bootstrap_snapshot_and_all_bumps(self):
        # No history dependency: Actions checks out only the latest commit.
        audit = json.loads(bootstrap_audit.AUDIT.read_text())
        self.assertEqual(len({p["manifest"] for p in audit["packages"]}), 115)
        self.assertEqual(bootstrap_audit.check(bootstrap_audit.REPO_ROOT, audit), [])
        self.assertTrue(any(p["name"] == "traceAI-openai" and p["status"] == "patch_required" for p in audit["packages"]))


if __name__ == "__main__":
    unittest.main()
