"""The plugin against Hermes itself, when hermes-agent is installed in this environment.

Skipped otherwise; the E2E run in the docs recipe covers a real session.
"""

import importlib.metadata
import threading

import pytest

from conftest import FakeContext, run_tool_turn
from traceai_hermes_agent._recorder import HOOKS


def test_entry_point_is_declared():
    eps = importlib.metadata.entry_points(group="hermes_agent.plugins")
    assert {(ep.name, ep.value) for ep in eps} >= {("futureagi", "traceai_hermes_agent:register")}


def test_every_hook_is_a_valid_hermes_hook():
    plugins = pytest.importorskip("hermes_cli.plugins")
    assert set(HOOKS) <= set(plugins.VALID_HOOKS)


def test_hermes_plugin_manager_loads_the_entry_point(monkeypatch, tmp_path):
    pytest.importorskip("hermes_cli.plugins_discovery")
    from hermes_cli.plugins_discovery import discover_entrypoint_manifests

    manifests = {m.key: m for m in discover_entrypoint_manifests()}
    assert "futureagi" in manifests
    assert manifests["futureagi"].source == "entrypoint"


def test_hooks_from_many_threads(make_hermes, exporter):
    ctx = make_hermes()
    threads = [threading.Thread(target=run_tool_turn, args=(ctx,),
                                kwargs={"session_id": "s{0}".format(i), "turn_id": "t{0}".format(i)})
               for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    spans = exporter.get_finished_spans()
    assert len(spans) == 48
    turns = {s.context.span_id: s for s in spans if s.name == "hermes.turn"}
    for span in spans:
        if span.name != "hermes.turn":
            parent = turns[span.parent.span_id]
            assert parent.attributes["session.id"] == span.attributes["session.id"]
