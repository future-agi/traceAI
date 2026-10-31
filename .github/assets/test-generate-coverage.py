#!/usr/bin/env python3
"""Coverage regression tests; run with Python 3.11+, no third-party packages."""
from pathlib import Path
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
NS = "http://maven.apache.org/POM/4.0.0"


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        paths = [ROOT / "README.md", ROOT / "java/pom.xml",
                 ROOT / ".github/assets/generate-coverage.py",
                 ROOT / ".github/assets/integrations/provenance.json"]
        for pattern in ("python/frameworks/*/pyproject.toml",
                        "typescript/packages/*/package.json",
                        "java/*/pom.xml", "go/*/go.mod", "csharp/src/*/*.csproj"):
            paths.extend(ROOT.glob(pattern))
        for source in paths:
            target = self.root / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    def generate(self, success=True):
        result = subprocess.run(
            [sys.executable, "-I", str(self.root / ".github/assets/generate-coverage.py")],
            capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        if success:
            return json.loads((self.root / ".github/assets/integration-coverage.json").read_text())
        return result

    def add_module(self, module, packaging="jar"):
        parent_path = self.root / "java/pom.xml"
        parent = ET.parse(parent_path)
        modules = parent.getroot().find(f"{{{NS}}}modules")
        if module not in [e.text for e in modules]:
            ET.SubElement(modules, f"{{{NS}}}module").text = module
        parent.write(parent_path, encoding="unicode")
        path = self.root / "java" / module / "pom.xml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'<project xmlns="{NS}"><modelVersion>4.0.0</modelVersion>'
                        f'<artifactId>{Path(module).name}</artifactId>'
                        f'<packaging>{packaging}</packaging></project>')

    def assert_same_coverage(self, before, after):
        for field in ("integration_families", "package_counts_in_matrix", "families"):
            self.assertEqual(before[field], after[field], field)

    def test_idempotent(self):
        self.generate()
        outputs = [self.root / "README.md", self.root / ".github/assets/integration-coverage.json"]
        before = [p.read_bytes() for p in outputs]
        self.generate()
        self.assertEqual(before, [p.read_bytes() for p in outputs])

    def test_full_catalog_visible_once_with_language_links(self):
        data = self.generate()
        readme = (self.root / "README.md").read_text()
        section = readme.split("## Integrations\n", 1)[1].split("\n## ", 1)[0]
        self.assertNotRegex(section, r"(?i)<\s*/?\s*(?:details|summary)\b")
        self.assertIn(f'**{data["integration_families"]} integration families · 5 languages', readme)
        self.assertEqual(data["integration_families"], 78)
        cognee = next(family for family in data["families"] if family["id"] == "cognee")
        self.assertEqual(cognee["category"], "rag-vector-databases")
        self.assertEqual(cognee["languages"]["Python"][0]["package"], "traceai-cognee")
        self.assertEqual(len(data["categories"]), 9)
        keys = [key for category in data["categories"] for key in category["families"]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(keys, [f["id"] for f in data["families"]])
        lines = [line for line in section.splitlines() if line.startswith('| <img ')]
        self.assertEqual(len(lines), data["integration_families"])
        header = "| Integration | Python | TypeScript | Java | Go | C# |"
        self.assertEqual(section.count(header), len(data["categories"]))
        for category in data["categories"]:
            self.assertIn(f'<a id="{category["id"]}"></a>\n\n### {category["name"]}', section)
            self.assertIn(f'[{category["name"]}](#{category["id"]})', section)
            group = section.split(f'### {category["name"]}\n', 1)[1].split("\n### ", 1)[0]
            self.assertEqual(group.count('| <img '), len(category["families"]))
        for line, family in zip(lines, data["families"]):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            self.assertEqual(len(cells), 6)
            self.assertTrue(cells[0].endswith(f' {family["name"]}'))
            self.assertEqual(cells[-1], "—")  # C# is manual spans, not auto-instrumentation.
            logo = re.search(r'<img src="([^"]+)"', cells[0])[1]
            self.assertTrue((ROOT / logo).is_file(), logo)
            for cell, language in zip(cells[1:], data["languages"]):
                packages = family["languages"].get(language, [])
                if not packages:
                    self.assertEqual(cell, "—")
                for package in packages:
                    self.assertIn(f']({package["source"]} "{package["package"]}")', cell)
                    self.assertTrue((ROOT / package["manifest"]).is_file())
            self.assertIn(family["id"], next(c for c in data["categories"]
                                           if c["id"] == family["category"])["families"])

    def test_named_integration_must_be_categorized(self):
        self.add_module("traceai-java-newprovider")
        generator = self.root / ".github/assets/generate-coverage.py"
        generator.write_text(generator.read_text().replace(
            "NAMES = {\n", 'NAMES = {\n    "newprovider": "New Provider",\n', 1))
        result = self.generate(success=False)
        self.assertIn("Categorize the new integration family: newprovider", result.stderr)

    def test_family_cannot_appear_in_multiple_categories(self):
        generator = self.root / ".github/assets/generate-coverage.py"
        generator.write_text(generator.read_text().replace(
            "category_members = [", 'CATEGORIES[1] = (*CATEGORIES[1][:3], CATEGORIES[1][3] + " openai")\ncategory_members = [', 1))
        result = self.generate(success=False)
        self.assertIn("An integration is in multiple categories", result.stderr)

    def test_bom_and_aggregator_not_integrations(self):
        before = self.generate()
        for name in ("traceai-bom", "traceai-platform"):
            self.add_module(name, "pom")
        after = self.generate()
        self.assert_same_coverage(before, after)
        excluded = {p["manifest"] for p in after["excluded_shared_packages"]}
        for name in ("traceai-bom", "traceai-platform"):
            self.assertIn(f"java/{name}/pom.xml", excluded)

    def test_jar_shared_and_examples_not_integrations(self):
        before = self.generate()
        for name in ("traceai-core", "traceai-java-bom", "traceai-java-openai-example", "examples"):
            self.add_module(name)
        self.assert_same_coverage(before, self.generate())

    def test_new_integration_must_be_named(self):
        self.add_module("traceai-java-newprovider")
        result = self.generate(success=False)
        self.assertIn("Name the new integration family: newprovider", result.stderr)


if __name__ == "__main__":
    unittest.main()
