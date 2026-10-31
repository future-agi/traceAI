"""Regression coverage for the one-time unpublished-fix audit (no network)."""
import json
import os
import subprocess
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

    def test_real_merge_counts_backdated_branch_commits_by_first_parent_landing_time(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)

            def git(*args, date=None):
                env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1',
                       'GIT_AUTHOR_NAME': 'Release test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
                       'GIT_COMMITTER_NAME': 'Release test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'}
                if date:
                    env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
                return subprocess.check_output(['git', '-C', str(root), *args], text=True, env=env).strip()

            git('init', '-b', 'main')
            file = root / self.package['manifest']
            file.parent.mkdir(parents=True)
            file.write_text('[project]\nname="traceAI-openai"\nversion="0.1.10"\n')
            git('add', '.'); git('commit', '-m', 'initial', date='2020-01-01T00:00:00Z')
            git('checkout', '-b', 'old-branch')
            (file.parent / 'fix.py').write_text('fixed = True\n')
            git('add', '.'); git('commit', '-m', 'fix: old authored change', date='2020-01-02T00:00:00Z')
            old_sha = git('rev-parse', 'HEAD')
            git('checkout', 'main')
            (root / 'README').write_text('main\n')
            git('add', '.'); git('commit', '-m', 'advance main', date='2026-03-09T00:00:00Z')
            # The package's current version uploaded on March 10. The branch's
            # author AND committer dates are earlier; it only lands on March 11.
            git('merge', '--no-ff', 'old-branch', '-m', 'Merge old branch', date='2026-03-11T00:00:00Z')
            merge_sha = git('rev-parse', 'HEAD')
            # Also backdate the next first-parent commit: --since-as-filter must
            # traverse through it, rather than stopping early like --since.
            (root / 'README').write_text('bootstrap\n')
            git('add', '.'); git('commit', '-m', 'bootstrap', date='2026-03-09T00:00:00Z')
            baseline = git('rev-parse', 'HEAD')
            history = bootstrap_audit.package_history(root, baseline, self.package, '2026-03-10T13:54:41Z')
            self.assertEqual([p['sha'] for p in history], [merge_sha])
            self.assertNotIn(old_sha, [p['sha'] for p in history])
            self.assertEqual(history[0]['committed_at'].replace('Z', '+00:00'), '2026-03-11T00:00:00+00:00')
            with patch.object(bootstrap_audit, '_get_json', return_value=self.registry):
                audit = bootstrap_audit.audit_package(root, baseline, self.package, {})
            self.assertEqual((audit['status'], audit['next_version']), ('patch_required', '0.1.11'))

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
        self.assertEqual(packages[0]["release_path"], "python")

    def test_committed_audit_covers_bootstrap_snapshot_and_all_bumps(self):
        # No history dependency: Actions checks out only the latest commit.
        audit = json.loads(bootstrap_audit.AUDIT.read_text())
        self.assertEqual(len({p["manifest"] for p in audit["packages"]}), 115)
        self.assertEqual(bootstrap_audit.check(bootstrap_audit.REPO_ROOT, audit), [])
        self.assertTrue(any(p["name"] == "traceAI-openai" and p["status"] == "patch_required" for p in audit["packages"]))


if __name__ == "__main__":
    unittest.main()
