"""The promptflow registry row, planned and enabled on the first import of ``promptflow.tracing``.

Prompt flow ships ``promptflow`` as a namespace package (no ``promptflow/__init__.py``), so the
integration is keyed on the ``promptflow.tracing`` sub-package. These tests use stand-ins and install
no real Prompt flow.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from fi_instrumentation.auto import _manager, _registry
from test_auto_manager import _ENVIRONMENT, _forget_modules, _reset_global_provider, _write_packages

PYTHON_ROOT = Path(__file__).resolve().parents[1]
PTH_FILE = PYTHON_ROOT / "fi_instrumentation_auto.pth"

# A stand-in traceAI package that records each instrument() call.
PROMPTFLOW_INSTRUMENTOR = '''
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor

CALLS = []


class PromptflowInstrumentor(BaseInstrumentor):
    def instrumentation_dependencies(self):
        return ()

    def _instrument(self, **kwargs):
        CALLS.append(kwargs)

    def _uninstrument(self, **kwargs):
        pass
'''

_STAND_INS = ("promptflow", "traceai_promptflow")


def _write_promptflow(root):
    """``promptflow.tracing`` without ``promptflow/__init__.py``, and the traceAI package for it."""
    (root / "promptflow" / "tracing").mkdir(parents=True)
    (root / "promptflow" / "tracing" / "__init__.py").write_text("")
    _write_packages(root, {"traceai_promptflow": PROMPTFLOW_INSTRUMENTOR})
    assert not (root / "promptflow" / "__init__.py").exists()


def _row():
    return next(i for i in _registry.ALL if i.name == "promptflow")


def test_registry_row_targets_the_tracing_subpackage():
    row = _row()
    assert row is next(i for i in _registry.INTEGRATIONS if i.name == "promptflow")
    assert row.targets == ("promptflow.tracing",)
    assert row.module == "traceai_promptflow"
    assert row.entry == "PromptflowInstrumentor"


@pytest.fixture
def stand_ins(monkeypatch, tmp_path):
    _write_promptflow(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(_manager, "_state", _manager._State())
    for key in _ENVIRONMENT:
        monkeypatch.delenv(key, raising=False)
    _forget_modules(_STAND_INS)
    _reset_global_provider()
    yield tmp_path
    _manager.shutdown()
    _reset_global_provider()
    _forget_modules(_STAND_INS)


def test_namespace_promptflow_is_detected_and_planned(stand_ins):
    status = {s.name: s for s in _manager.plan()}["promptflow"]
    assert status.status == _manager.PENDING
    assert "promptflow.tracing" in status.detail
    # Planning imports nothing, and the stand-in really is a namespace package.
    assert "promptflow" not in sys.modules
    assert "traceai_promptflow" not in sys.modules
    import promptflow

    assert getattr(promptflow, "__file__", None) is None
    assert promptflow.__spec__.origin is None


def test_promptflow_without_its_traceai_package_is_not_traced_yet(stand_ins):
    (stand_ins / "traceai_promptflow" / "__init__.py").unlink()
    (stand_ins / "traceai_promptflow").rmdir()
    status = {s.name: s for s in _manager.plan()}["promptflow"]
    assert status.status == _manager.MISSING_INSTRUMENTOR
    assert "pip install traceAI-promptflow" in status.detail


def test_zero_code_activation_on_the_first_import_of_promptflow_tracing(tmp_path):
    _write_promptflow(tmp_path)
    script = tmp_path / "check.py"
    script.write_text(textwrap.dedent('''
        import json, sys
        exec(open(sys.argv[1]).read())  # the .pth line, as the interpreter runs it at startup

        def calls():
            module = sys.modules.get("traceai_promptflow")
            return len(module.CALLS) if module else 0

        before = calls()
        import promptflow.tracing
        after_first = calls()
        import promptflow.tracing  # noqa: F811
        print(json.dumps({
            "before": before,
            "after_first": after_first,
            "after_second": calls(),
            "namespace": getattr(sys.modules["promptflow"], "__file__", None) is None,
        }))
    '''))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FI_", "OTEL_"))}
    env.update({
        "PYTHONPATH": os.pathsep.join([str(tmp_path), str(PYTHON_ROOT)]),
        "FI_AUTO_INSTRUMENT": "1",
        "FI_BASE_URL": "http://127.0.0.1:9",
        "FI_API_KEY": "test-api-key",
        "FI_SECRET_KEY": "test-secret-key",
    })
    result = subprocess.run([sys.executable, str(script), str(PTH_FILE)], env=env,
                            capture_output=True, text=True, timeout=120, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "before": 0, "after_first": 1, "after_second": 1, "namespace": True,
    }
