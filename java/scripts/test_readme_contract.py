#!/usr/bin/env python3
"""Regression checks for release-managed README coordinates (no provider calls)."""
import json
from pathlib import Path
import runpy
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
CONSUMERS = runpy.run_path(str(ROOT / "scripts/test_consumers.py"))


class ReadmeContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.readme = (ROOT / "README.md").read_text()
        cls.version = ET.parse(ROOT / "pom.xml").getroot().findtext(
            "{http://maven.apache.org/POM/4.0.0}version")

    def test_advertised_versions_match_reactor(self):
        self.assertEqual(CONSUMERS["readme_version"](self.readme, self.version), self.version)

    def test_each_install_coordinate_is_release_managed(self):
        marked = [line for line in self.readme.splitlines() if "x-release-please-version" in line]
        self.assertEqual(len(marked), 5)
        updated = self.readme
        for line in marked:
            self.assertIn(self.version, line)
            updated = updated.replace(line, line.replace(self.version, "9.8.7"))
        self.assertEqual(CONSUMERS["readme_version"](updated, "9.8.7"), "9.8.7")
        # Historical releases, JitPack pins and provider SDK versions must not change.
        unmarked_before = [line for line in self.readme.splitlines() if "x-release-please-version" not in line]
        unmarked_after = [line for line in updated.splitlines() if "x-release-please-version" not in line]
        self.assertEqual(unmarked_before, unmarked_after)

    def test_readme_is_registered_with_release_please(self):
        config = json.loads((ROOT.parent / "release-please-config.json").read_text())
        self.assertIn({"type": "generic", "path": "README.md"}, config["packages"]["java"]["extra-files"])

    def test_stale_or_missing_coordinates_are_rejected(self):
        for label, old in [
            ("Maven BOM", f"<artifactId>traceai-bom</artifactId>\n            <version>{self.version}</version>"),
            ("Gradle BOM", f"com.futureagi:traceai-bom:{self.version}"),
            ("direct core", f"<artifactId>traceai-java-core</artifactId>\n    <version>{self.version}</version>"),
            ("without BOM", f"Without the BOM, specify `<version>{self.version}</version>`"),
        ]:
            with self.subTest(coordinate=label):
                self.assertIn(old, self.readme)
                stale = self.readme.replace(old, old.replace(self.version, "0.0.0"))
                with self.assertRaisesRegex(ValueError, "do not match reactor"):
                    CONSUMERS["readme_version"](stale, self.version)
                with self.assertRaisesRegex(ValueError, "missing"):
                    CONSUMERS["readme_version"](self.readme.replace(old, ""), self.version)

    def test_exact_ownership_example_is_compiled(self):
        source = CONSUMERS["readme_consumer"](self.readme, "### Provider ownership and shutdown")
        snippet = self.readme.split("### Provider ownership and shutdown", 1)[1]
        snippet = snippet.split("```java\n", 1)[1].split("```", 1)[0]
        for line in snippet.splitlines():
            self.assertIn(line, source)
        self.assertIn(".build(), GlobalOpenTelemetry.get());", source)

    def test_immutable_releases_are_not_claimed_to_have_ownership_api(self):
        # Keep the warning until a containing release passes --central --core-only.
        self.assertRegex(self.readme, r"Unreleased; source build required")
        self.assertIn("Central `1.0.0` and `1.0.1` do not provide this overload", self.readme)
        self.assertIn("test_consumers.py --core-only", self.readme)
        self.assertIn("test_consumers.py --central --core-only", self.readme)
        self.assertNotIn("prepared for the first Central release", self.readme)


if __name__ == "__main__":
    unittest.main()
