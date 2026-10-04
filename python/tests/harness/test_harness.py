"""Contract tests for the shared loopback-only harness."""

import json
import sys

import pytest

from tests.harness import Receiver, compare, post_otlp, run


def test_receiver_stores_posted_span_and_can_clear() -> None:
    payload = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "trace-id",
                                "spanId": "span-id",
                                "name": "harness.receiver",
                                "attributes": [
                                    {"key": "component", "value": {"stringValue": "test"}}
                                ],
                                "status": {"code": 1},
                            }
                        ]
                    }
                ]
            }
        ]
    }

    with Receiver() as receiver:
        assert post_otlp(payload, receiver.endpoint) == 200
        assert receiver.spans() == payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
        receiver.clear()
        assert receiver.spans() == []


def test_receiver_accepts_the_collector_path() -> None:
    payload = {
        "resourceSpans": [
            {"scopeSpans": [{"spans": [{"name": "collector.path"}]}]}
        ]
    }

    with Receiver() as receiver:
        assert post_otlp(payload, receiver.collector_endpoint) == 200
        assert receiver.spans() == [{"name": "collector.path"}]


def test_run_captures_output_and_kills_timed_out_process() -> None:
    completed = run(
        [
            sys.executable,
            "-c",
            "import sys; print('stdout'); sys.stderr.write('stderr\\n')",
        ],
        env={},
        stdin=b"",
        timeout=1,
    )
    assert completed.returncode == 0
    assert completed.stdout == b"stdout\n"
    assert completed.stderr == b"stderr\n"
    assert completed.timed_out is False

    timed_out = run(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env={},
        stdin=b"",
        timeout=0.1,
    )
    assert timed_out.timed_out is True
    assert timed_out.returncode != 0


def test_compare_accepts_normalized_match_and_prints_mismatch_diff(tmp_path, capsys) -> None:
    golden = [
        {
            "traceId": "golden-trace",
            "spanId": "golden-span",
            "startTimeUnixNano": "1",
            "endTimeUnixNano": "2",
            "name": "harness.compare",
            "attributes": [
                {"key": "alpha", "value": {"stringValue": "a"}},
                {"key": "beta", "value": {"intValue": "2"}},
            ],
            "status": {"code": 1},
        }
    ]
    golden_path = tmp_path / "spans.golden.json"
    golden_path.write_text(json.dumps(golden), encoding="utf-8")

    actual = [
        {
            "traceId": "other-trace",
            "spanId": "other-span",
            "startTimeUnixNano": "3",
            "endTimeUnixNano": "4",
            "name": "harness.compare",
            "attributes": list(reversed(golden[0]["attributes"])),
            "status": {"code": 1},
        }
    ]
    compare(actual, golden_path)

    actual[0]["name"] = "harness.mismatch"
    with pytest.raises(AssertionError, match="spans do not match golden"):
        compare(actual, golden_path)
    assert "---" in capsys.readouterr().out
