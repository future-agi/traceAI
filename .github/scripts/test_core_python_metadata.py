"""Keep the core distribution's Python range aligned with what it supports.

The Python CI Plan job runs these standard-library-only checks before installing
packages. EvalTag uses PEP 604 unions at runtime, so Python 3.9 cannot import core.
The distribution's ceiling covers core tracing, including Python 3.15.
Optional evaluation dependencies have their own compatibility matrix and must
neither lower that ceiling nor silently disappear on newer Python runtimes.
"""

import re
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

    def test_python_ceiling_matches_core_qualification(self):
        self.assertRegex(self.poetry["dependencies"]["python"], r",<3\.16$")

    def test_evaluation_stays_optional_without_runtime_omission(self):
        for name in ("futureagi", "ai-evaluation"):
            dependency = self.poetry["dependencies"][name]
            self.assertTrue(dependency["optional"])
            self.assertNotIn("python", dependency)
            self.assertNotIn("markers", dependency)
            self.assertIn(name, self.poetry["extras"]["evaluation"])

    def test_classifiers_match_the_declared_range(self):
        minors = sorted(
            int(match.group(1))
            for classifier in self.poetry["classifiers"]
            if (
                match := re.fullmatch(
                    r"Programming Language :: Python :: 3\.(\d+)", classifier
                )
            )
        )
        self.assertEqual(minors, list(range(10, 16)))

    def test_lockfile_python_range_matches_distribution(self):
        lock = tomllib.loads((CORE / "poetry.lock").read_text())
        self.assertEqual(
            lock["metadata"]["python-versions"],
            self.poetry["dependencies"]["python"],
        )


if __name__ == "__main__":
    unittest.main()
