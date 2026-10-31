"""Tests of the release planner, on a small repository laid out like this one (no network)."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import release_notes  # noqa: E402
import release_plan  # noqa: E402


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class FakeRepo:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write(self.root / "python/pyproject.toml", '[tool.poetry]\nname = "fi-instrumentation-otel"\nversion = "1.1.0"\npackages = [\n    { include = "fi_instrumentation" },\n]\n')
        write(self.root / "python/fi_instrumentation/__init__.py", '__version__ = "1.1.0"\n')
        write(self.root / "python/frameworks/openai/pyproject.toml", '[tool.poetry]\nname = "traceAI-openai"\nversion = "0.1.10"\npackages = [{ include = "traceai_openai" }]\n')
        write(self.root / "python/frameworks/openai/traceai_openai/version.py", '__version__ = "0.1.10"\n')
        write(self.root / "python/frameworks/openai/CHANGELOG.md", "# Changelog\n\n## [0.1.10] - 2026-03-10\n### Fixed\n- Something.\n\n## [0.1.9] - 2026-01-01\n- Older.\n")
        write(self.root / "python/frameworks/agno/pyproject.toml", '[project]\nname = "traceai-agno"\nversion = "0.1.0"\n\n[tool.hatch.build.targets.wheel]\npackages = ["traceai_agno"]\n')
        write(self.root / "python/frameworks/agno/traceai_agno/__init__.py", "x = 1\n")
        write(self.root / "python/frameworks/tests_e2e/README.md", "not a package\n")
        write(self.root / "python/traceai/pyproject.toml", '[tool.poetry]\nname = "traceai"\nversion = "0.1.0"\n')
        write(self.root / "typescript/packages/fi-core/package.json", json.dumps({"name": "@traceai/fi-core", "version": "1.0.0"}))
        write(self.root / "typescript/packages/traceai_openai/package.json", json.dumps({"name": "@traceai/openai", "version": "0.1.13"}))
        write(self.root / "typescript/packages/e2e/package.json", json.dumps({"name": "traceai-e2e-tests", "version": "0.0.0", "private": True}))


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.repo = FakeRepo()
        self.packages = release_plan.python_packages(self.repo.root) + release_plan.typescript_packages(self.repo.root)

    def tearDown(self):
        self.repo.tmp.cleanup()

    def test_discovery_names_versions_and_tags(self):
        by = {p["name"]: p for p in self.packages}
        self.assertEqual(sorted(by), ["@traceai/fi-core", "@traceai/openai", "fi-instrumentation-otel", "traceAI-openai", "traceai", "traceai-agno"])
        self.assertEqual(by["traceAI-openai"]["tag"], "traceai-openai-v0.1.10")
        self.assertEqual(by["@traceai/openai"]["tag"], "npm-traceai-openai-v0.1.13")
        self.assertEqual(by["fi-instrumentation-otel"]["import_name"], "fi_instrumentation")
        self.assertEqual(by["traceai-agno"]["import_name"], "traceai_agno")
        self.assertTrue(by["fi-instrumentation-otel"]["core"] and by["@traceai/fi-core"]["core"])
        self.assertFalse(by["traceAI-openai"]["core"])
        self.assertEqual(by["traceAI-openai"]["changelog"], "python/frameworks/openai/CHANGELOG.md")
        self.assertEqual(by["traceAI-openai"]["declared"], {"traceai_openai/version.py": "0.1.10"})

    def test_consistency_catches_version_drift(self):
        self.assertEqual(release_plan.consistency_problems(self.packages), [])
        write(self.repo.root / "python/frameworks/openai/traceai_openai/version.py", '__version__ = "0.1.0"\n')
        packages = release_plan.python_packages(self.repo.root)
        self.assertEqual(release_plan.consistency_problems(packages), ["python/frameworks/openai/traceai_openai/version.py: __version__ is 0.1.0 but pyproject.toml says 0.1.10"])

    def test_npm_runtime_version_constants_are_checked(self):
        pkg = self.repo.root / "typescript/packages/traceai_openai"
        write(pkg / "src/version.ts", 'export const VERSION = "0.1.13"; // x-release-please-version\n')
        packages = release_plan.typescript_packages(self.repo.root)
        by = {p["name"]: p for p in packages}
        self.assertEqual(by["@traceai/openai"]["declared"], {"src/version.ts": "0.1.13"})
        self.assertEqual(by["@traceai/fi-core"]["declared"], {})
        self.assertEqual(release_plan.consistency_problems(packages), [])
        write(pkg / "src/version.ts", "export const VERSION = '0.1.0';\n")
        packages = release_plan.typescript_packages(self.repo.root)
        self.assertEqual(release_plan.consistency_problems(packages), ["typescript/packages/traceai_openai/src/version.ts: VERSION is 0.1.0 but package.json says 0.1.13"])

    def test_npm_runtime_version_needs_marker_and_extra_file(self):
        pkg = self.repo.root / "typescript/packages/traceai_openai"
        write(pkg / "src/version.ts", 'export const VERSION = "0.1.13";\n')
        write(self.repo.root / "release-please-config.json", json.dumps({"packages": {"typescript/packages/traceai_openai": {"release-type": "node"}}}))
        write(self.repo.root / ".release-please-manifest.json", json.dumps({"typescript/packages/traceai_openai": "0.1.13"}))
        packages = [p for p in release_plan.typescript_packages(self.repo.root) if p["name"] == "@traceai/openai"]
        self.assertEqual(release_plan.coverage_problems(self.repo.root, packages), [
            "typescript/packages/traceai_openai/src/version.ts: missing release-please version marker",
            "typescript/packages/traceai_openai/src/version.ts: missing release-please extra-file",
        ])
        write(pkg / "src/version.ts", 'export const VERSION = "0.1.13"; // x-release-please-version\n')
        write(self.repo.root / "release-please-config.json", json.dumps({"packages": {"typescript/packages/traceai_openai": {"release-type": "node", "extra-files": ["src/version.ts"]}}}))
        self.assertEqual(release_plan.coverage_problems(self.repo.root, packages), [])

    def test_offline_plan_lists_everything_core_first_and_honours_skips(self):
        cfg = {"skip": {"traceai": "the PyPI name belongs to another publisher"}}
        p = release_plan.plan(self.packages, cfg, offline=True)
        self.assertEqual([e["name"] for e in p["python"]], ["fi-instrumentation-otel", "traceai-agno", "traceAI-openai"])
        self.assertEqual([e["name"] for e in p["typescript"]], ["@traceai/fi-core", "@traceai/openai"])
        self.assertEqual(p["skipped"], [{"name": "traceai", "reason": "the PyPI name belongs to another publisher"}])
        self.assertNotIn("declared", p["python"][0])

    def test_plan_skips_published_versions(self):
        have = {"traceAI-openai": {"0.1.9", "0.1.10"}, "fi-instrumentation-otel": {"1.0.0"}, "@traceai/openai": None}
        with patch.object(release_plan, 'published_versions', side_effect=lambda p: have.get(p["name"], set())):
            p = release_plan.plan(self.packages, {"skip": {}}, only={"traceAI-openai", "fi-instrumentation-otel", "@traceai/openai"})
        self.assertEqual([e["name"] for e in p["python"]], ["fi-instrumentation-otel"])
        self.assertEqual([(e["name"], e["first_release"]) for e in p["typescript"]], [("@traceai/openai", True)])
        self.assertEqual(p["skipped"], [{"name": "traceAI-openai", "reason": "0.1.10 is already published"}])

    def test_bootstrap_token_only_for_listed_projects_never_published(self):
        have = {"traceai-agno": None, "traceAI-openai": None, "fi-instrumentation-otel": {"1.0.0"}}
        cfg = {"skip": {}, "pypi_bootstrap_token": ["traceai-agno", "fi-instrumentation-otel"]}
        with patch.object(release_plan, 'published_versions', side_effect=lambda p: have.get(p["name"], set())):
            p = release_plan.plan(self.packages, cfg, only={"traceai-agno", "traceAI-openai", "fi-instrumentation-otel"})
        self.assertEqual({e["name"]: e["pypi_bootstrap"] for e in p["python"]},
                         {"fi-instrumentation-otel": False, "traceai-agno": True, "traceAI-openai": False})

    def test_check_mode_exit_codes(self):
        self.assertEqual(release_plan.main(["--check", "--root", str(self.repo.root)]), 0)
        write(self.repo.root / "python/fi_instrumentation/__init__.py", '__version__ = "0.9"\n')
        self.assertEqual(release_plan.main(["--check", "--root", str(self.repo.root)]), 1)


class NotesTests(unittest.TestCase):
    def test_changelog_section_is_extracted(self):
        text = "# Changelog\n\n## [Unreleased]\n- wip\n\n## [0.1.10] - 2026-03-10\n### Fixed\n- Something.\n\n## [0.1.9]\n- Older.\n"
        self.assertEqual(release_notes.section(text, "0.1.10"), "### Fixed\n- Something.")
        self.assertIsNone(release_notes.section(text, "0.2.0"))
        self.assertEqual(release_notes.section("## 0.1.9 (2026-01-01)\n- Older.\n", "0.1.9"), "- Older.")

    def test_notes_fall_back_when_there_is_no_section(self):
        n = release_notes.notes("traceAI-openai", "0.2.0", changelog_text=None, install="pip install traceAI-openai==0.2.0")
        self.assertIn("pip install traceAI-openai==0.2.0", n)
        self.assertIn("no CHANGELOG entry", n)


if __name__ == "__main__":
    unittest.main()
