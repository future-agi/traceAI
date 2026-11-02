"""Span-only disposal must be idempotent and safe during garbage collection."""

import gc
import sys
from unittest.mock import Mock

import pytest
from opentelemetry.trace import Span, StatusCode
from traceai_vllm._wrappers import _StreamSpan


@pytest.mark.parametrize("error", [None, RuntimeError("synthetic stream failure")])
def test_disposal_does_not_end_or_overwrite_a_finished_span(error):
    span = Mock(spec=Span)
    tracking = _StreamSpan(span)
    tracking.finish(error)
    del tracking
    gc.collect()
    span.end.assert_called_once_with()
    span.set_attributes.assert_called_once()
    span.set_status.assert_called_once()
    expected = StatusCode.OK if error is None else StatusCode.ERROR
    assert span.set_status.call_args.args[0].status_code == expected
    if error is None:
        span.record_exception.assert_not_called()
    else:
        span.record_exception.assert_called_once_with(error)


def test_disposal_does_not_leak_tracing_failures(monkeypatch):
    unraisable = []
    monkeypatch.setattr(sys, "unraisablehook", unraisable.append)
    span = Mock(spec=Span)
    span.end.side_effect = RuntimeError("synthetic exporter failure")
    tracking = _StreamSpan(span)
    del tracking
    gc.collect()
    span.end.assert_called_once_with()
    assert not unraisable
