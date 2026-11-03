"""Conservative per-choice completion accounting without a request terminator."""

import pytest
from traceai_openwebui._response import _MAX_FRAME, Observation


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
        observation = Observation(span, False, 10, choice_count=1)
        observation.payload({"choices": [{"index": 0, "finish_reason": "stop"}]})
        assert observation.terminal
        # A provider sent more choices than requested. EOF is not proof the
        # unexpected choice completed, even though the expected one did.
        observation.payload(
            {"choices": [{"index": 1, "delta": {"content": "private"}}]}
        )
        observation.finish()
        assert not observation.terminal


@pytest.mark.parametrize("choice", [{"index": 0}, {}])
def test_unknown_choice_count_defaults_to_requiring_done(setup_pipe, choice):
    with setup_pipe.provider.get_tracer("test").start_as_current_span("test") as span:
        observation = Observation(span, False, 10)
        observation.payload({"choices": [{**choice, "finish_reason": "stop"}]})
        observation.finish()
        assert not observation.terminal
        observation.chunk("data: [DONE]\n\n")
        observation.finish()
        assert observation.terminal


def test_single_choice_without_index_can_finish(setup_pipe):
    with setup_pipe.provider.get_tracer("test").start_as_current_span("test") as span:
        observation = Observation(span, False, 10, choice_count=1)
        observation.payload({"choices": [{"finish_reason": "stop"}]})
        observation.finish()
        assert observation.terminal


@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.parametrize("chunk_size", [8191, _MAX_FRAME + 100])
@pytest.mark.parametrize("multiline", [False, True])
def test_observation_limit_boundary(setup_pipe, extra, chunk_size, multiline):
    prefix = 'data: {\ndata: "choices":' if multiline else 'data: {"choices":'
    prefix += '[{"delta":{"content":"'
    suffix = '"},"finish_reason":"stop"}]}\n\n'
    # The observation budget counts decoded characters, not UTF-8 bytes.
    text = prefix + "é" * (_MAX_FRAME + extra - len(prefix + suffix)) + suffix
    wire = (text + "data: [DONE]\n\n").encode()
    with setup_pipe.provider.get_tracer("test").start_as_current_span("test") as span:
        observation = Observation(span, True, 10, choice_count=1)
        for offset in range(0, len(wire), chunk_size):
            observation.chunk(wire[offset : offset + chunk_size])
            assert (
                len(observation._buffer) + sum(map(len, observation._data))
                <= _MAX_FRAME
            )
        observation.finish()
        assert observation.terminal is (extra == 0)
        assert observation.output == ("é" * 10 if extra == 0 else "")
    (exported,) = setup_pipe.exporter.get_finished_spans()
    assert exported.attributes.get("openwebui.observation_lost", False) is (extra > 0)


@pytest.mark.parametrize(
    "terminator",
    ["data: [DONE]\n\n", 'data: {"choices":[{"finish_reason":"stop"}]}\n\n'],
)
@pytest.mark.parametrize("position", ["before", "after", "unterminated"])
def test_observation_loss_survives_completion_and_finish(
    setup_pipe, terminator, position
):
    with setup_pipe.provider.get_tracer("test").start_as_current_span("test") as span:
        observation = Observation(span, False, 10, choice_count=1)
        if position != "after":
            observation.chunk(terminator)
            assert observation.terminal
        # Feed strings in pieces too: no data from this event may be retained.
        observation.chunk('data: {"error":{"message":"private-error-')
        for _ in range(17):
            observation.chunk("x" * 65536)
        assert not observation._buffer and not observation._data
        if position != "unterminated":
            observation.chunk('"}}\n\n' + terminator)
        observation.finish()
        observation.finish()  # EOF and span cleanup both finalize the observer.
        assert not observation.terminal
        assert (
            not observation.failed
        )  # Lost observation is not a proven provider error.
    (exported,) = setup_pipe.exporter.get_finished_spans()
    assert exported.attributes["openwebui.observation_lost"] is True
    assert "private-error" not in repr(exported.attributes)
