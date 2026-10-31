"""Install-stage preparation for the required offline dependency-resolution test.

Run before entering the test network namespace, never from pytest:
  python tests/prepare_resolution_wheelhouse.py --wheelhouse /tmp/lc-wheels
The test consumes only these wheels with pip --no-index --ignore-installed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

PACKAGE = Path(__file__).resolve().parents[1]


def source_digest():
    digest = hashlib.sha256()
    for path in sorted([PACKAGE / "pyproject.toml", *PACKAGE.glob("traceai_langchain/**/*.py")]):
        digest.update(path.relative_to(PACKAGE).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    args = parser.parse_args()
    wheels = args.wheelhouse.resolve()
    wheels.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheels), str(PACKAGE)], check=True)
    source_wheel, = wheels.glob("traceai_langchain-*.whl")
    subprocess.run([sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--dest", str(wheels), str(source_wheel), "langchain==1.0.2", "langgraph==1.0.1"], check=True)
    manifest = {"source_sha256": source_digest(), "wheels": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in wheels.glob("*.whl")}}
    (wheels / "resolution-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
