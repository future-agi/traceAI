#!/usr/bin/env python3
"""
E2E Test Runner

Runs E2E tests with configurable options.
"""

import argparse
import subprocess
import sys
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Run E2E tests for TraceAI")
    parser.add_argument(
        "--suite",
        choices=["all", "sdk", "api", "db", "usecases"],
        default="all",
        help="Test suite to run",
    )
    parser.add_argument(
        "--sdk",
        choices=["openai", "anthropic", "groq", "langchain", "litellm", "all"],
        default="all",
        help="Specific SDK to test (for sdk suite)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose output",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Run with coverage",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print command without running",
    )
    parser.add_argument(
        "--markers", "-m",
        help="Pytest markers to filter tests",
    )

    args = parser.parse_args()

    # Build pytest command
    cmd = ["python", "-m", "pytest"]

    # Add test path based on suite
    test_dir = Path(__file__).parent
    if args.suite == "all":
        cmd.append(str(test_dir))
    elif args.suite == "sdk":
        if args.sdk == "all":
            cmd.append(str(test_dir / "sdk"))
        else:
            cmd.append(str(test_dir / "sdk" / f"test_sdk_{args.sdk}.py"))
    elif args.suite == "api":
        cmd.append(str(test_dir / "api"))
    elif args.suite == "db":
        cmd.append(str(test_dir / "db"))
    elif args.suite == "usecases":
        cmd.append(str(test_dir / "usecases"))

    # Add options
    if args.verbose:
        cmd.append("-v")

    cmd.append("--tb=short")

    if args.coverage:
        cmd.extend(["--cov=tracer", "--cov-report=term-missing"])

    if args.markers:
        cmd.extend(["-m", args.markers])

    # Print command
    print(f"Running: {' '.join(cmd)}")

    if args.dry_run:
        return 0

    # Run tests
    result = subprocess.run(cmd, cwd=str(test_dir.parent))
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
