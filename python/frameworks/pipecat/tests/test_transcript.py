import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from traceai_pipecat import BaseMappedSpanExporter
from traceai_pipecat import _transcript


KEY = "fi.conversation.transcript"
OBSERVER = "pipecat.evals.conversation_observer"
FLAGS = (
    "hide_inputs", "hide_input_messages", "hide_input_text",
    "hide_outputs", "hide_output_messages", "hide_output_text",
)
TURNS = [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi"}]


class Observer:
    def event_handler(self, name):
        assert name == "on_conversation_ended"

        def register(callback):
            self.finish = lambda rows: asyncio.run(callback(self, SimpleNamespace(conversation=rows)))
            return callback

        return register


@pytest.fixture(autouse=True)
def defaults(monkeypatch):
    for flag in FLAGS:
        monkeypatch.setenv("FI_" + flag.upper(), "false")
    monkeypatch.delitem(sys.modules, "fi_instrumentation.auto._manager", raising=False)
    monkeypatch.setitem(sys.modules, OBSERVER, SimpleNamespace(EvalConversationObserver=Observer))
    # This process installed the hook, as BaseMappedSpanExporter does.
    monkeypatch.setenv(_transcript._OWNER, str(os.getpid()))


@pytest.fixture
def span():
    provider = TracerProvider()
    root = provider.get_tracer(__name__).start_span("conversation")
    yield root
    if root.is_recording():
        root.end()
    provider.shutdown()


def worker(span=None, setup=_transcript.setup_pipeline_worker):
    w = SimpleNamespace(turn_trace_observer=SimpleNamespace(_conversation_span=span), add_observer=Mock())
    asyncio.run(setup(w))
    return w


def observer(w):
    w.add_observer.assert_called_once()
    return w.add_observer.call_args.args[0]


def load_setup(path):
    # Pipecat imports setup files by path, outside their original package.
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mapping_appends_loadable_hook_once_and_honors_shared_policy(monkeypatch):
    previous = ["first.py", "second.py"]
    monkeypatch.setenv("PIPECAT_SETUP_FILES", os.pathsep.join(previous))
    BaseMappedSpanExporter()
    BaseMappedSpanExporter()
    *existing, path = os.environ["PIPECAT_SETUP_FILES"].split(os.pathsep)
    assert existing == previous
    assert Path(path).is_absolute()
    policy = Mock(return_value=False)
    shared = SimpleNamespace(allows_conversation_transcript=policy)
    monkeypatch.setitem(sys.modules, "fi_instrumentation.instrumentation.transcript", shared)
    setup = load_setup(Path(path))
    assert asyncio.run(setup.setup_worker_runner(object())) is None
    w = worker(setup=setup.setup_pipeline_worker)
    policy.assert_called_once()
    assert isinstance(policy.call_args.args[0], TraceConfig)
    w.add_observer.assert_not_called()


def test_ordered_root_transcript_discards_tool_rows_and_extra_fields(span):
    w = worker()  # Pipecat creates the span after setting up its observers.
    w.turn_trace_observer._conversation_span = span
    observer(w).finish([dict(TURNS[0], timestamp=23), {"role": "tool_call", "name": "lookup"}, TURNS[1]])
    assert json.loads(span.attributes[KEY]) == TURNS


@pytest.mark.parametrize("flag", FLAGS)
def test_older_core_hide_flags_prevent_capture(monkeypatch, flag, span):
    # None makes the import fail, as on a core release without the helper.
    monkeypatch.setitem(sys.modules, "fi_instrumentation.instrumentation.transcript", None)
    monkeypatch.setenv("FI_" + flag.upper(), "true")
    w = worker(span, load_setup(Path(_transcript.__file__)).setup_pipeline_worker)
    w.add_observer.assert_not_called()
    assert KEY not in span.attributes


def test_uses_effective_config_to_mask_both_roles(monkeypatch, span):
    class SideMask(TraceConfig):
        def mask(self, key, value):
            return {"input.value": "masked input", "output.value": "masked output"}[key]

    manager = SimpleNamespace(_state=SimpleNamespace(trace_config=SideMask()))
    monkeypatch.setitem(sys.modules, "fi_instrumentation.auto._manager", manager)
    observer(worker(span)).finish(TURNS)
    assert json.loads(span.attributes[KEY]) == [
        {"role": "user", "content": "masked input"},
        {"role": "assistant", "content": "masked output"},
    ]


def test_failed_mask_omits_entire_transcript(monkeypatch, span):
    mask = Mock(side_effect=["first masked", ValueError("mask failed")])
    monkeypatch.setattr(TraceConfig, "mask", mask)
    observer(worker(span)).finish(TURNS)
    assert KEY not in span.attributes


def test_empty_after_masking_omits_attribute(monkeypatch, span):
    monkeypatch.setattr(TraceConfig, "mask", lambda *args: "")
    observer(worker(span)).finish(TURNS)
    assert KEY not in span.attributes


@pytest.mark.parametrize("eligible_span", ["child", "ended", "missing"])
def test_no_write_without_a_recording_root(eligible_span):
    candidate = Mock()
    candidate.parent = object() if eligible_span == "child" else None
    candidate.is_recording.return_value = eligible_span != "ended"
    w = worker(None if eligible_span == "missing" else candidate)
    observer(w).finish(None)  # An ineligible span must return without even iterating the turns.
    candidate.set_attribute.assert_not_called()


def test_worker_without_tracing_or_native_observer_is_unchanged(monkeypatch):
    untraced = SimpleNamespace(turn_trace_observer=None, add_observer=Mock())
    asyncio.run(_transcript.setup_pipeline_worker(untraced))
    untraced.add_observer.assert_not_called()
    monkeypatch.setitem(sys.modules, OBSERVER, None)
    worker(setup=_transcript.setup_pipeline_task).add_observer.assert_not_called()


def test_inherited_setup_file_does_nothing_in_a_process_that_did_not_install_it(monkeypatch, span):
    monkeypatch.setenv(_transcript._OWNER, str(os.getpid() + 1))
    w = worker(span)
    w.add_observer.assert_not_called()
    assert KEY not in span.attributes


def test_installing_marks_this_process_as_the_owner(monkeypatch):
    monkeypatch.delenv(_transcript._OWNER, raising=False)
    monkeypatch.delenv("PIPECAT_SETUP_FILES", raising=False)
    _transcript._install_setup_file()
    assert os.environ[_transcript._OWNER] == str(os.getpid())


class WatchedObserver(Observer):
    async def on_push_frame(self, data):
        self.frames = getattr(self, "frames", 0) + 1


@pytest.mark.parametrize("rotated", [False, True])
def test_turns_from_another_root_are_never_written(monkeypatch, span, rotated):
    monkeypatch.setitem(sys.modules, OBSERVER, SimpleNamespace(EvalConversationObserver=WatchedObserver))
    provider = TracerProvider()
    earlier = provider.get_tracer(__name__).start_span("conversation")
    w = worker(earlier if rotated else span)
    obs = observer(w)
    asyncio.run(obs.on_push_frame(object()))
    w.turn_trace_observer._conversation_span = span
    asyncio.run(obs.on_push_frame(object()))
    obs.finish(TURNS)
    assert obs.frames == 2
    if rotated:
        assert KEY not in span.attributes
    else:
        assert json.loads(span.attributes[KEY]) == TURNS
    earlier.end()
    provider.shutdown()


@pytest.mark.parametrize("limit", [10, None])
def test_transcript_over_the_span_value_limit_is_omitted_not_cut(limit):
    from opentelemetry.sdk.trace import SpanLimits

    provider = TracerProvider(span_limits=SpanLimits(max_span_attribute_length=limit))
    root = provider.get_tracer(__name__).start_span("conversation")
    w = worker(root)
    observer(w).finish(TURNS)
    if limit is None:
        assert json.loads(root.attributes[KEY]) == TURNS
    else:
        assert KEY not in root.attributes
    root.end()
    provider.shutdown()
