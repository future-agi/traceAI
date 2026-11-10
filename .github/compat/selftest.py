#!/usr/bin/env python3
"""Secret-free canary unit/negative controls (no registry or provider calls).

The workflow additionally builds/installs actual pilot SDKs from the frozen
registry fixtures and runs the source SHA's real contracts inside namespaces.
"""
import argparse
import pathlib
import subprocess
import sys
import unittest

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=pathlib.Path,
                        help='Also run PR1 canonical probes from this trusted source checkout')
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.discover(str(pathlib.Path(__file__).parent), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = not result.wasSuccessful()
    if args.source_root:
        canonical = args.source_root.resolve() / '.github/compat'
        if not (canonical / 'validate.py').is_file() or not (canonical / 'test_validate.py').is_file():
            parser.error('Combined selftest requires PR1 validator and original probes')
        # A separate interpreter avoids test-module/import collisions between the
        # producer and source checkout. No files or assertions are overwritten.
        completed = subprocess.run([sys.executable, '-I', '-m', 'unittest', 'discover',
            '-s', str(canonical), '-p', 'test_validate.py', '-v'], check=False, timeout=300)
        failed = failed or completed.returncode != 0
    sys.exit(failed)
