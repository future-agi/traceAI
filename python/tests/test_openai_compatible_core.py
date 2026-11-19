"""The default-on rules for OpenAI-compatible providers, with no provider package and no httpx installed.

Everything here runs in the core jobs, which install only fi-instrumentation-otel. Packages are simulated
on a temporary import path; the marker reader is exercised on source text. The span-level behaviour across the
real openai and provider packages is tested in python/tests_cross_package/openai_compatible.
"""

import sys
import textwrap

import pytest
from fi_instrumentation.auto import _manager, _registry
from fi_instrumentation.auto._config import AutoConfig

DEFERRING = ("deepseek", "fireworks", "xai", "minimax", "vllm")
EXTENSIONS = ("_supports_deepseek_extensions",)
MINIMAX = ("_supports_minimax_provider",)


def _integration(name):
    return next(i for i in _registry.ALL if i.name == name)


@pytest.mark.parametrize("name", DEFERRING)
def test_registry_turns_provider_on_by_default(name):
    integration = _integration(name)
    assert integration.default is True
    assert integration.note == ""
    assert integration.shared_openai is True


def _write(root, name, source):
    package = root / name
    package.mkdir()
    (package / "__init__.py").write_text(textwrap.dedent(source))


@pytest.fixture
def site(tmp_path, monkeypatch):
    """An empty import path in front of everything else; packages are added with ``install``."""
    for name in ("openai", "traceai_openai", *(_integration(n).module for n in DEFERRING), "defers_probe"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.syspath_prepend(str(tmp_path))

    class Site:
        root = tmp_path

        @staticmethod
        def install(name, source=""):
            _write(tmp_path, name, source or "VALUE = 1\n")

        @staticmethod
        def provider(name, marker=True):
            _write(tmp_path, _integration(name).module, f"DEFERS_TO_SHARED_OPENAI = {marker}\n")

        @staticmethod
        def shared_openai(*capabilities):
            body = "".join(f"    {flag} = True\n" for flag in capabilities) or "    pass\n"
            _write(tmp_path, "traceai_openai", f"class OpenAIInstrumentor:\n{body}")

    Site.install("openai")
    return Site


def _enabled_by_default(names):
    settings = AutoConfig()
    return [name for name in names if _manager._plan(_integration(name), settings, lazy=False)[1] == "now"]


# --- the marker reader ---------------------------------------------------------------------------


def _probe(tmp_path, monkeypatch, source):
    root = tmp_path / "defers_probe"
    root.mkdir()
    (root / "__init__.py").write_text(textwrap.dedent(source))
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "defers_probe", raising=False)
    return _registry.Integration("probe", ("openai",), "probe", "defers_probe", "Probe")


def test_marker_is_read_without_importing_the_package(tmp_path, monkeypatch):
    integration = _probe(tmp_path, monkeypatch, """
        DEFERS_TO_SHARED_OPENAI = True
        raise RuntimeError("the package must not be executed to read its marker")
    """)
    assert _manager.provider_defers_to_shared_openai(integration) is True
    assert "defers_probe" not in sys.modules


@pytest.mark.parametrize("source", ["VALUE = 1\n", "DEFERS_TO_SHARED_OPENAI = False\n", "# DEFERS_TO_SHARED_OPENAI = True\n"])
def test_missing_or_false_marker_does_not_defer(tmp_path, monkeypatch, source):
    assert _manager.provider_defers_to_shared_openai(_probe(tmp_path, monkeypatch, source)) is False


def test_missing_package_does_not_defer():
    integration = _registry.Integration("probe", ("openai",), "probe", "defers_probe_not_installed", "Probe")
    assert _manager.provider_defers_to_shared_openai(integration) is False


@pytest.mark.parametrize("source", [
    '"""Docs.\n\n    DEFERS_TO_SHARED_OPENAI = True\n"""\n',
    '"""Docs.\nDEFERS_TO_SHARED_OPENAI = True\n"""\n',
    "def f():\n    DEFERS_TO_SHARED_OPENAI = True\n",
    "class C:\n    DEFERS_TO_SHARED_OPENAI = True\n",
    'text = "DEFERS_TO_SHARED_OPENAI = True"\n',
])
def test_marker_only_counts_as_a_top_level_assignment(tmp_path, monkeypatch, source):
    assert _manager.provider_defers_to_shared_openai(_probe(tmp_path, monkeypatch, source)) is False


def test_top_level_marker_after_a_docstring_reads_true(tmp_path, monkeypatch):
    integration = _probe(tmp_path, monkeypatch, '"""Docs."""\n\nDEFERS_TO_SHARED_OPENAI = True\n')
    assert _manager.provider_defers_to_shared_openai(integration) is True


def test_an_unreadable_source_declares_nothing(tmp_path, monkeypatch):
    integration = _probe(tmp_path, monkeypatch, "DEFERS_TO_SHARED_OPENAI = True\n")
    (tmp_path / "defers_probe" / "__init__.py").write_bytes(b"\xff\xfe = (\n")
    assert _manager.provider_defers_to_shared_openai(integration) is False


# --- version skew, with simulated packages -------------------------------------------------------


@pytest.mark.parametrize("name", DEFERRING)
def test_provider_without_the_marker_keeps_its_old_state(name, site):
    site.provider(name, marker=False)
    site.shared_openai(*EXTENSIONS, *MINIMAX)
    assert name not in _enabled_by_default([name])
    status = next(s for s in _manager.plan(AutoConfig()) if s.name == name)
    assert status.status == _manager.DISABLED
    assert "already traces these calls" in status.detail


@pytest.mark.parametrize("name", DEFERRING)
def test_provider_with_the_marker_is_on_by_default(name, site):
    site.provider(name)
    site.shared_openai(*EXTENSIONS, *MINIMAX)
    assert name in _enabled_by_default([name])


@pytest.mark.parametrize("name", DEFERRING)
def test_listing_a_provider_explicitly_still_selects_it(name, site):
    site.provider(name, marker=False)
    site.shared_openai()
    settings = AutoConfig(instrumentations=frozenset({name}))
    assert _manager._plan(_integration(name), settings, lazy=False)[1] == "now"


def test_providers_that_need_the_shared_extensions_stay_off_with_an_older_traceai_openai(site):
    for name in DEFERRING:
        site.provider(name)
    site.shared_openai()  # no capability declared
    enabled = _enabled_by_default(DEFERRING)
    for name in ("deepseek", "xai", "minimax"):
        assert name not in enabled, name
    for name in ("fireworks", "vllm"):
        assert name in enabled, name


def test_minimax_needs_the_shared_minimax_labels(site):
    for name in DEFERRING:
        site.provider(name)
    site.shared_openai(*EXTENSIONS)
    enabled = _enabled_by_default(DEFERRING)
    assert "minimax" not in enabled
    assert "xai" in enabled


@pytest.mark.parametrize("name", ["deepseek", "xai", "minimax"])
def test_provider_is_on_by_default_when_traceai_openai_is_not_installed(name, site):
    site.provider(name)
    assert name in _enabled_by_default([name])
    status = next(s for s in _manager.plan(AutoConfig()) if s.name == name)
    assert "already traces" not in status.detail


def test_unmarked_provider_stays_off_even_without_traceai_openai(site):
    site.provider("xai", marker=False)
    assert "xai" not in _enabled_by_default(["xai"])


# --- teardown order, with stub instrumentors -----------------------------------------------------


def test_cleanups_run_in_reverse_order():
    calls = []
    _manager._run_cleanups({"a": lambda: calls.append("a"), "b": lambda: calls.append("b"), "c": lambda: calls.append("c")})
    assert calls == ["c", "b", "a"]


def test_a_failing_cleanup_does_not_stop_the_others():
    calls = []

    def broken():
        raise RuntimeError("synthetic")

    _manager._run_cleanups({"a": lambda: calls.append("a"), "b": broken, "c": lambda: calls.append("c")})
    assert calls == ["c", "a"]


def test_shutdown_undoes_every_stub_instrumentor_newest_first(monkeypatch):
    calls = []
    state = _manager._State()
    state.cleanups = {name: (lambda n=name: calls.append(n)) for name in ("openai", "deepseek", "xai")}
    monkeypatch.setattr(_manager, "_state", state)
    _manager.shutdown(timeout_millis=5000)
    assert calls == ["xai", "deepseek", "openai"]
    assert state.stopped and state.cleanups == {}
