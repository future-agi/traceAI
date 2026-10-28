#!/usr/bin/env python3
"""Secret-free canary unit/negative controls (no registry or provider calls).

The workflow additionally builds/installs actual pilot SDKs from the frozen
registry fixtures and runs the source SHA's real contracts inside namespaces.
"""
import pathlib
import sys
import unittest

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(str(pathlib.Path(__file__).parent), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
