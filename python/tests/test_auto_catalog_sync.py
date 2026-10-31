"""The packaged wire catalog must be the spec's catalog, byte for byte."""

from pathlib import Path

import fi_instrumentation.auto.wire._core as core

SPEC = Path(__file__).resolve().parents[2] / "spec" / "auto-instrumentation" / "wire-protocols.json"


def test_packaged_catalog_matches_spec():
    assert core._CATALOG_PATH.read_bytes() == SPEC.read_bytes(), (
        "run: python spec/auto-instrumentation/sync.py --write"
    )
