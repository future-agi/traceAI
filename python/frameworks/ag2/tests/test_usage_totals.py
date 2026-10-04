"""Trace token totals against AG2's own ``UsageReport``, with real AG2.

fi-collector promotes ``gen_ai.usage.input_tokens`` / ``output_tokens`` /
``total_tokens`` into its token columns on any span, and Observe sums them over
every span of a trace. So the sum of those keys over a trace must equal the
total AG2 itself reports for the run. The model is AG2's scripted
``ag2.testing.TestConfig``: no network, no API key.
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterable, Tuple

import pytest

from traceai_ag2 import setup

from ._support import attrs, emits_record_usage, memory_provider

PROMOTED_INPUT = "gen_ai.usage.input_tokens"
PROMOTED_OUTPUT = "gen_ai.usage.output_tokens"
PROMOTED_TOTAL = "gen_ai.usage.total_tokens"

needs_record_usage = pytest.mark.skipif(
    not emits_record_usage(),
    reason="record_usage spans (the only carrier of aggregation/compaction/sub-task rollup spend) start at ag2 1.0.3",
)


def _ag2_version() -> Tuple[int, ...]:
    from importlib.metadata import version

    parts = []
    for piece in version("ag2").split(".")[:3]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


def _response(text: str, prompt_tokens: int, completion_tokens: int) -> Any:
    from ag2.events import ModelMessage, ModelResponse
    from ag2.usage import Usage

    return ModelResponse(
        ModelMessage(text),
        usage=Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        model="fake-model",
    )


def _trace_sums(spans: Iterable[Any]) -> Tuple[int, int, int]:
    spans = list(spans)
    assert len({s.context.trace_id for s in spans}) == 1, "expected one trace"
    total_in = sum(attrs(s).get(PROMOTED_INPUT, 0) for s in spans)
    total_out = sum(attrs(s).get(PROMOTED_OUTPUT, 0) for s in spans)
    total = sum(attrs(s).get(PROMOTED_TOTAL, 0) for s in spans)
    return total_in, total_out, total


def _run(agent: Any, prompt: str) -> Any:
    async def _go() -> Any:
        reply = await agent.ask(prompt)
        return await reply.usage()

    return asyncio.run(_go())


def _assert_trace_matches_report(spans: Any, report: Any) -> None:
    total_in, total_out, total = _trace_sums(spans)
    assert (total_in, total_out) == (report.total.prompt_tokens, report.total.completion_tokens)
    # AG2 never sets gen_ai.usage.total_tokens; if a later release does, it must
    # not be counted twice either.
    assert total in (0, report.total.prompt_tokens + report.total.completion_tokens)


def _knowledge(*, aggregate: bool = False, compact: bool = False) -> Any:
    from ag2.knowledge import MemoryKnowledgeStore
    from ag2.knowledge.config import KnowledgeConfig
    from ag2.testing import TestConfig

    kwargs: dict = {"store": MemoryKnowledgeStore(), "expose_tool": False, "write_event_log": False}
    if aggregate:
        from ag2.aggregate import AggregateTrigger, ConversationSummaryAggregate

        kwargs["aggregate"] = ConversationSummaryAggregate(TestConfig(_response("summary", 100, 50)))
        kwargs["aggregate_trigger"] = AggregateTrigger(on_end=True)
    if compact:
        from ag2.compact import CompactTrigger, SummarizeCompact

        kwargs["compact"] = SummarizeCompact(target=2, config=TestConfig(_response("compacted", 300, 30)))
        kwargs["compact_trigger"] = CompactTrigger(max_events=2)
    return KnowledgeConfig(**kwargs)


def _delegating_planner(worker: Any, **planner_kwargs: Any) -> Any:
    from ag2 import Agent
    from ag2.events import ToolCallEvent
    from ag2.testing import TestConfig
    from ag2.tools.subagents import subagent_tool

    return Agent(
        "planner",
        config=TestConfig(
            ToolCallEvent("task_worker", arguments='{"objective": "x"}', id="call_task_1"),
            _response("ok", 11, 7),
        ),
        tools=[subagent_tool(worker, description="delegate")],
        **planner_kwargs,
    )


def _worker() -> Any:
    from ag2 import Agent
    from ag2.testing import TestConfig

    return Agent("worker", config=TestConfig(_response("done", 40, 4)))


# --- R1: memory aggregation calls the model outside on_llm_call ---------------


@needs_record_usage
def test_aggregation_usage_counts_once_in_trace_total():
    """aggregate.py calls the client on a throwaway stream: no chat span repeats it."""
    from ag2 import Agent
    from ag2.testing import TestConfig

    provider, exporter = memory_provider()
    agent = Agent("mem_bot", config=TestConfig(_response("hello", 11, 7)), knowledge=_knowledge(aggregate=True))
    setup(agent, tracer_provider=provider)
    report = _run(agent, "hi")

    assert (report.total.prompt_tokens, report.total.completion_tokens) == (111, 57)
    spans = exporter.get_finished_spans()
    (aggregation,) = [s for s in spans if s.name == "record_usage aggregation"]
    assert attrs(aggregation)[PROMOTED_INPUT] == 100
    assert attrs(aggregation)[PROMOTED_OUTPUT] == 50
    _assert_trace_matches_report(spans, report)


# --- R2: a sub-task rollup repeats an instrumented worker's chat spans --------


@needs_record_usage
def test_subtask_rollup_keeps_tokens_when_worker_is_not_instrumented():
    """The rollup is the only copy of the worker's spend: it must stay promoted."""
    provider, exporter = memory_provider()
    worker = _worker()
    planner = _delegating_planner(worker)
    setup(planner, tracer_provider=provider)
    report = _run(planner, "plan")

    spans = exporter.get_finished_spans()
    (rollup,) = [s for s in spans if s.name == "record_usage subtask"]
    assert attrs(rollup)["ag2.usage.label"] == "worker"
    assert attrs(rollup)[PROMOTED_INPUT] == 40
    assert attrs(rollup)[PROMOTED_OUTPUT] == 4
    _assert_trace_matches_report(spans, report)


def test_subtask_rollup_counts_once_when_worker_is_instrumented():
    """setup(planner, worker): the worker's chat spans already carry its tokens."""
    provider, exporter = memory_provider()
    worker = _worker()
    planner = _delegating_planner(worker)
    setup(planner, worker, tracer_provider=provider)
    report = _run(planner, "plan")

    spans = exporter.get_finished_spans()
    assert [s.name for s in spans if s.name.startswith("invoke_agent")] == ["invoke_agent worker", "invoke_agent planner"]
    for rollup in [s for s in spans if s.name == "record_usage subtask"]:
        a = attrs(rollup)
        assert PROMOTED_INPUT not in a and PROMOTED_OUTPUT not in a
        assert a["ag2.usage.input_tokens"] == 40
        assert a["ag2.usage.output_tokens"] == 4
    _assert_trace_matches_report(spans, report)


@needs_record_usage
def test_subtask_rollup_for_an_agent_seen_in_another_trace_keeps_tokens():
    """Agent names are tracked per trace: an instrumented worker's earlier,
    separate run must not demote a later rollup from an uninstrumented copy."""
    from ag2 import Agent
    from ag2.testing import TestConfig

    provider, exporter = memory_provider()
    instrumented_namesake = Agent("worker", config=TestConfig(_response("solo", 1, 1)))
    setup(instrumented_namesake, tracer_provider=provider)
    _run(instrumented_namesake, "warm up")
    exporter.clear()

    planner = _delegating_planner(_worker())
    setup(planner, tracer_provider=provider)
    report = _run(planner, "plan")
    _assert_trace_matches_report(exporter.get_finished_spans(), report)


# --- every usage route in one run ---------------------------------------------


@needs_record_usage
@pytest.mark.parametrize("instrument_worker", [False, True], ids=["worker-uninstrumented", "worker-instrumented"])
def test_trace_tokens_equal_usage_report_across_every_usage_route(instrument_worker):
    """Main loop (chat + record_usage model_call), compaction, aggregation and a
    sub-task rollup in one run: summed promoted input and output tokens equal
    AG2's UsageReport.

    Compaction is left out on ag2 < 1.0.4: there ``UsageReport`` reads the
    compacted history, so it drops usage events compaction removed (measured on
    1.0.3: report 111 input tokens, trace and actual spend 451). The trace is
    right in that case and the report is not, so it is no ground truth.
    """
    compact = _ag2_version() >= (1, 0, 4)
    provider, exporter = memory_provider()
    worker = _worker()
    planner = _delegating_planner(worker, knowledge=_knowledge(aggregate=True, compact=compact))
    agents = (planner, worker) if instrument_worker else (planner,)
    setup(*agents, tracer_provider=provider)
    report = _run(planner, "plan")

    spans = exporter.get_finished_spans()
    names = {s.name for s in spans}
    expected_routes = ["record_usage model_call", "record_usage aggregation", "record_usage subtask"]
    if compact:
        expected_routes.append("record_usage compaction")
    for expected in expected_routes:
        assert expected in names, f"{expected} not emitted; the scenario no longer covers it"
    spent_in = 11 + 40 + 100 + (300 if compact else 0)
    spent_out = 7 + 4 + 50 + (30 if compact else 0)
    assert (report.total.prompt_tokens, report.total.completion_tokens) == (spent_in, spent_out)
    _assert_trace_matches_report(spans, report)
