"""Fixtures for the cross-package OpenAI-compatible provider tests.

These tests need openai, httpx, traceai-openai and the five provider packages. They run in the
openai-compatible-providers workflow, which installs them; a missing package must fail, never skip.
"""

import pytest


@pytest.fixture(autouse=True)
def _collector_at_a_closed_local_port(monkeypatch):
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FI_GRPC_URL", "http://127.0.0.1:9")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """A skipped test would hide a missing install: report it as a failure."""
    outcome = yield
    report = outcome.get_result()
    if report.skipped:
        report.outcome = "failed"
        report.longrepr = f"skipped in the cross-package job, which must run every test: {report.longrepr}"
