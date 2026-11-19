"""Keep core package version bumps paired with their release manifest entries."""
import json
import tomllib
import unittest
from pathlib import Path


class CoreReleaseManifestTests(unittest.TestCase):
    def test_core_package_versions_match_release_manifest(self):
        root = Path(__file__).resolve().parents[2]
        manifest = json.loads((root / ".release-please-manifest.json").read_text())
        python_data = tomllib.loads((root / "python/pyproject.toml").read_text())
        python_package = python_data.get("project", python_data.get("tool", {}).get("poetry", {}))
        typescript_package = json.loads((root / "typescript/packages/fi-core/package.json").read_text())
        for release_path, expected_name, package in (
            ("python", "fi-instrumentation-otel", python_package),
            ("typescript/packages/fi-core", "@traceai/fi-core", typescript_package),
        ):
            with self.subTest(release_path=release_path):
                self.assertEqual(package["name"], expected_name)
                self.assertEqual(
                    manifest[release_path],
                    package["version"],
                    f"{release_path}: update .release-please-manifest.json with the package version bump",
                )


if __name__ == "__main__":
    unittest.main()
