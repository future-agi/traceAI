"""Offline tests for live_check.py (no collector needed)."""

from __future__ import annotations

import json
from pathlib import Path

import live_check

HERE = Path(__file__).parent
GOLDEN = json.loads((HERE / "columns.golden.json").read_text())


def rows_from_golden() -> list[dict]:
    rows = []
    for spans in GOLDEN["columns"].values():
        for name, cols in spans.items():
            rows.append({"name": name, **{k: cols[k] for k in live_check.COLUMNS}})
    return rows


def test_golden_rows_pass() -> None:
    assert live_check.check(rows_from_golden(), GOLDEN) == []


def test_summed_tokens_fail() -> None:
    rows = rows_from_golden()
    next(r for r in rows if r["name"] == "dual_emit")["prompt_tokens"] = 6
    assert any("prompt_tokens" in p for p in live_check.check(rows, GOLDEN))


def test_response_model_fails() -> None:
    rows = rows_from_golden()
    next(r for r in rows if r["name"] == "chat gpt-test")["model"] = "gpt-test-actual"
    assert live_check.check(rows, GOLDEN)


def test_missing_span_fails() -> None:
    rows = [r for r in rows_from_golden() if r["name"] != "retrieval"]
    assert any("not stored" in p for p in live_check.check(rows, GOLDEN))


def test_recorded_live_rows_match_golden() -> None:
    # Real rows exported from a local stack on 2026-10-16 (fi_verify --json).
    sample = HERE / "live_rows.sample.json"
    assert live_check.check(json.loads(sample.read_text()), GOLDEN) == []
