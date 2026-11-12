"""Each auto test owns its provider/registry lifecycle."""
import pytest
import traceai


@pytest.fixture(autouse=True)
def isolated_auto_state(monkeypatch):
    monkeypatch.setattr(traceai, "_tracer_provider", None)
    monkeypatch.setattr(traceai, "_init_options", None)
    monkeypatch.setattr(traceai, "_instrumented", set())
    monkeypatch.setattr(traceai, "_adapter_status", {})
    monkeypatch.setattr(traceai, "_attachment_manager_borrowed", False)
