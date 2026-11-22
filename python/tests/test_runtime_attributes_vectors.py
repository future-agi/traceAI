"""Python writer and TypeScript consumer normalize carriers identically.

Loads the same processor_v1.json vectors the fi-core TypeScript tests run, so a
change to either implementation's caps, count derivation or truncation fails here.
"""

import json
from pathlib import Path

import pytest

from fi_instrumentation.instrumentation.runtime_attributes import _attributes

VECTORS = (
    Path(__file__).resolve().parents[2]
    / "typescript/packages/fi-core/src/__tests__/vectors/processor_v1.json"
)
# Carrier-only vectors: registry fallback and explicit span values are consumer rules.
CASES = [
    vector
    for vector in json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"]
    if isinstance(vector["carrier"], dict) and vector["carrier"].get("v") == 1 and not vector["explicit"]
]


def _plain(attrs):
    return {key: list(value) if isinstance(value, tuple) else value for key, value in attrs.items()}


@pytest.mark.parametrize("vector", CASES, ids=[vector["name"] for vector in CASES])
def test_python_normalizes_like_the_typescript_consumer(vector):
    assert _plain(_attributes(vector["carrier"]["attrs"])) == vector["expected"]


def test_shared_vectors_cover_capped_skill_lists():
    capped = [vector for vector in CASES if vector["expected"].get("fi.skills.truncated") is True]
    assert capped and all(vector["expected"]["fi.skills.count"] == 32 for vector in capped)


def test_keys_past_the_scan_cap_are_ignored():
    late = {f"x.k{i}": i for i in range(300)}
    late["fi.context.name"] = "late"
    assert _attributes(late) == {}
    early = {"fi.context.name": "early"}
    early.update({f"x.k{i}": i for i in range(300)})
    assert _attributes(early) == {"fi.context.name": "early"}
