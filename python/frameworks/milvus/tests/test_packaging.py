"""A normal installation must be able to import the declared PyMilvus floor."""

import subprocess
import sys


def test_pymilvus_imports_in_clean_interpreter():
    # CI installs each supported SDK release from scratch without hand-pinning
    # transitive dependencies; this catches incompatible resolver selections.
    subprocess.run(
        [sys.executable, "-I", "-c", "import pymilvus"],
        check=True,
        capture_output=True,
        text=True,
    )
