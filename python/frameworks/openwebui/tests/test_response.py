"""Conservative per-choice completion accounting without a request terminator."""

import pytest
from traceai_openwebui._response import Observation


@pytest.mark.parametrize(
    "count,choices",
    [
        (2, [{"finish_reason": "stop"}, {"finish_reason": "stop"}]),
        (2, [{"index": 0, "finish_reason": "stop"}] * 2),
        (1, [{"index": True, "finish_reason": "stop"}]),
        (1, [{"index": -1, "finish_reason": "stop"}]),
        (1, [{"index": 1, "finish_reason": "stop"}]),
        (1, [{"index": 0, "finish_reason": 42}]),
        (1, [{"index": 0, "finish_reason": ""}]),
        (1, [None]),
        (None, [{"index": 0, "finish_reason": "stop"}]),
        ("2", [{"index": 0, "finish_reason": "stop"}]),
        (True, [{"index": 0, "finish_reason": "stop"}]),
    ],
)
def test_ambiguous_or_unfinished_choices_need_request_terminator(
    setup_pipe, count, choices
):
    with setup_pipe.provider.get_tracer("test").start_as_current_span("test") as span:
        observation = Observation(span, False, 10, count)
        observation.payload({"choices": choices})
        observation.finish()
        assert not observation.terminal
        observation.chunk("data: [DONE]\n\n")
        observation.finish()
        assert observation.terminal


def test_additional_unfinished_choice_does_not_latch_completion(setup_pipe):
    with setup_pipe.provider.get_tracer("test").start_as_current_span("test") as span:
        observation = Observation(span, False, 10)
        observation.payload({"choices": [{"index": 0, "finish_reason": "stop"}]})
        assert observation.terminal
        # A provider sent more choices than requested. EOF is not proof the
        # unexpected choice completed, even though the expected one did.
        observation.payload(
            {"choices": [{"index": 1, "delta": {"content": "private"}}]}
        )
        observation.finish()
        assert not observation.terminal


def test_single_choice_without_index_can_finish(setup_pipe):
    with setup_pipe.provider.get_tracer("test").start_as_current_span("test") as span:
        observation = Observation(span, False, 10)
        observation.payload({"choices": [{"finish_reason": "stop"}]})
        observation.finish()
        assert observation.terminal
