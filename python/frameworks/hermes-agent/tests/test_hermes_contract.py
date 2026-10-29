"""Required offline contracts against the exact upstream Hermes dev dependency.

Missing plugin/package-manager modules are failures, not optional skips.
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
    from hermes_cli import plugins
    assert set(HOOKS) <= set(plugins.VALID_HOOKS)


def test_hermes_plugin_manager_loads_the_entry_point(monkeypatch, tmp_path):
    import hermes_cli.plugins_discovery
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


def test_real_entrypoint_loader_registers_real_plugin_context(tmp_path, monkeypatch, provider):
    from hermes_cli.plugins import PluginContext, PluginManager
    from hermes_cli.plugins_discovery import discover_entrypoint_manifests

    # Never discover user/project plugins or touch the operator's Hermes profile.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manifest = next(m for m in discover_entrypoint_manifests() if m.key == "futureagi")
    manager = PluginManager(scope_key=str(tmp_path))
    entrypoint = manager._load_entrypoint_module(manifest)
    context = PluginContext(manifest, manager)
    entrypoint(context, tracer_provider=provider)
    assert set(manager._hooks) == set(HOOKS)
    assert all(len(callbacks) == 1 for callbacks in manager._hooks.values())
