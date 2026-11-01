"""Keep the core distribution's Python floor aligned with its runtime syntax.

The Python CI Plan job runs these standard-library-only checks before installing
packages. EvalTag uses PEP 604 unions at runtime, so Python 3.9 cannot import core.
"""

import tomllib
import unittest
from pathlib import Path


CORE = Path(__file__).resolve().parents[2] / "python"


class CorePythonMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.poetry = tomllib.loads((CORE / "pyproject.toml").read_text())["tool"][
            "poetry"
        ]

    def test_python_floor_requires_310(self):
        self.assertRegex(self.poetry["dependencies"]["python"], r"^>=3\.10(?:,|$)")

    def test_classifiers_advertise_the_supported_floor(self):
        self.assertNotIn(
            "Programming Language :: Python :: 3.9", self.poetry["classifiers"]
        )
        self.assertIn(
            "Programming Language :: Python :: 3.10", self.poetry["classifiers"]
        )

    def test_lockfile_python_range_matches_distribution(self):
        lock = tomllib.loads((CORE / "poetry.lock").read_text())
        self.assertEqual(
            lock["metadata"]["python-versions"],
            self.poetry["dependencies"]["python"],
        )


if __name__ == "__main__":
    unittest.main()
