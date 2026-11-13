"""Check that Python install instructions name the published core distribution.

The core package is published as ``fi-instrumentation-otel``; its import namespace
is ``fi_instrumentation``. Install commands and dependency lists in the docs must
use the distribution name, not the import name.

Run with python3 -m unittest discover -s .github/scripts -p 'test_*.py'.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OLD_DISTRIBUTION = re.compile(r"(?<![\w-])fi-instrumentation(?:\[[^\]]+\])?(?![\w-])", re.I)
PIP_INSTALL = re.compile(r"\bpip(?:3)?\s+install\s+([^\n`]+)", re.I)
DEPENDENCY = re.compile(r"^\s*[-*]\s+fi-instrumentation(?:\[[^\]]+\])?(?:\s|$)", re.I)


def _documentation_files():
    return (
        [ROOT / "README.md"]
        + list((ROOT / "docs").rglob("*.md"))
        + list((ROOT / "python").rglob("*.md"))
    )


class DocumentedInstallNames(unittest.TestCase):
    def test_python_install_requirements_use_published_distribution(self):
        failures = []
        docs = [path for path in _documentation_files() if "node_modules" not in path.parts]
        for path in docs:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                commands = PIP_INSTALL.findall(line)
                if any(OLD_DISTRIBUTION.search(command) for command in commands) or DEPENDENCY.search(line):
                    failures.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
        self.assertTrue(docs)
        self.assertFalse(
            failures,
            "Install instructions use a name that is not the published distribution:\n"
            + "\n".join(failures),
        )

    def test_published_distribution_and_import_namespace_stay_distinct(self):
        metadata = (ROOT / "python" / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('name = "fi-instrumentation-otel"', metadata)
        self.assertIn('include = "fi_instrumentation"', metadata)
        self.assertIsNone(OLD_DISTRIBUTION.search('fi-instrumentation-otel[grpc]'))
        self.assertIsNotNone(OLD_DISTRIBUTION.search('fi-instrumentation[grpc]'))


if __name__ == "__main__":
    unittest.main()
