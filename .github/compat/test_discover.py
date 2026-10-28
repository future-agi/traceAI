import copy
import json
import unittest
from pathlib import Path

from discover import eligible, go, go_escape, npm, pypi, semver, summarize, timestamp

FIXTURES = Path(__file__).with_name("fixtures")
SEEN = "2026-10-28T00:00:00Z"
CUTOFF = timestamp("2026-10-25T00:00:00Z")


class DiscoveryTests(unittest.TestCase):
    def test_artifact_cooldown_yank_and_runtime(self):
        def file(date, yanked=False, python=">=3.10"):
            return dict(filename="sdk.whl", upload_time_iso_8601=date, yanked=yanked, requires_python=python)
        data = {"releases": {
            "1.9.0": [file("2026-10-01T00:00:00Z")],
            "1.10.0": [file("2026-10-01T00:00:00Z", True), file("2026-10-27T00:00:00Z")],
            "1.11.0": [file("2026-10-01T00:00:00Z", python=">=3.13")],
            "1.12.0": [file("2026-10-01T00:00:00Z", True)],
            "2.0.0rc1": [file("2026-10-01T00:00:00Z")], "garbage": []}}
        package = pypi("sdk", data, SEEN)
        summary = summarize(package, CUTOFF, "3.11")
        self.assertEqual(summary["absolute_latest"], "1.11.0")
        self.assertEqual(summary["latest_eligible"], "1.9.0")
        self.assertEqual(summarize(package, CUTOFF, "3.13")["latest_eligible"], "1.11.0")
        self.assertFalse(eligible(package["releases"][1], CUTOFF))

    def test_missing_pypi_publication_is_not_invented(self):
        package = pypi("sdk", {"releases": {"1.0.0": [{"filename": "sdk.whl"}]}}, SEEN)
        self.assertIsNone(package["releases"][0]["published_at"])
        self.assertFalse(eligible(package["releases"][0], CUTOFF))

    def test_npm_semver_latest_tag_is_independent_and_engines_preserved(self):
        raw = json.loads((FIXTURES / "npm.json").read_text())["@example/sdk"]
        package = npm("@example/sdk", raw, SEEN)
        summary = summarize(package, CUTOFF)
        self.assertEqual(summary["absolute_latest"], "1.10.0")
        self.assertEqual(summary["dist_tags"]["latest"], "1.9.0")
        self.assertEqual(package["releases"][1]["engines"], {"node": ">=20"})
        self.assertLess(semver("1.0.0-rc.2"), semver("1.0.0-rc.10"))
        self.assertLess(semver("1.0.0-rc.10"), semver("1.0.0"))

    def test_go_retractions_module_paths_and_metadata(self):
        raw = json.loads((FIXTURES / "go.json").read_text())["example.com/SDK/v2"]
        package = go("example.com/SDK/v2", raw, SEEN)
        self.assertEqual(go_escape("example.com/SDK/v2"), "example.com/!s!d!k/v2")
        retracted = [r["version"] for r in package["releases"] if r["retracted"]]
        self.assertEqual(retracted, ["v2.1.0", "v2.2.0", "v2.2.1", "v2.2.2"])
        self.assertEqual(summarize(package, CUTOFF)["latest_eligible"], "v2.3.0")
        self.assertIn("/vN", package["module_path_assessment"])
        self.assertTrue(all(r["module_path"].endswith("/v2") for r in package["releases"]))
