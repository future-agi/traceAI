#!/usr/bin/env python3
"""Coverage regression tests; run with Python 3.11+, no third-party packages."""
from pathlib import Path
import json
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
