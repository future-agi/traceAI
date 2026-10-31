"""Old allowed core versions must never turn optional telemetry into a startup error."""

import builtins
import importlib.util
import sys
from pathlib import Path

import pytest
import traceai_transformers


@pytest.mark.parametrize("missing", ["auto-package", "privacy-symbol"])
def test_legacy_core_missing_privacy_fails_open_before_hooks(fake_modules, monkeypatch, caplog, missing):
    root = Path(traceai_transformers.__file__).parent
    original_import = builtins.__import__

    def legacy_import(name, *args, **kwargs):
        if name == "fi_instrumentation.auto.wire._privacy":
            if missing == "auto-package":
                raise ModuleNotFoundError("No module named 'fi_instrumentation.auto'",
                                          name="fi_instrumentation.auto")
            raise ImportError("cannot import name 'mask_attributes'")
        return original_import(name, *args, **kwargs)

    # Execute fresh module bodies with the exact missing API of the old core,
    # without reloading shared modules or changing other tests' instrumentors.
    monkeypatch.setattr(builtins, "__import__", legacy_import)
    spec = importlib.util.spec_from_file_location("_legacy_transformers_wrappers", root / "_wrappers.py")
    wrappers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrappers)
    assert not wrappers.PRIVACY_AVAILABLE
    monkeypatch.setitem(sys.modules, "traceai_transformers._wrappers", wrappers)
    spec = importlib.util.spec_from_file_location("_legacy_transformers", root / "__init__.py")
    package = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package)

    original_generate = fake_modules.GenerationMixin.generate
    original_pipeline = fake_modules.Pipeline.__call__
    original_put = fake_modules.TextStreamer.put
    instrumentor = package.TransformersInstrumentor()
    try:
        instrumentor.instrument()
        assert not instrumentor._enabled and instrumentor._patches == []
        assert fake_modules.GenerationMixin.generate is original_generate
        assert fake_modules.Pipeline.__call__ is original_pipeline
        assert fake_modules.TextStreamer.put is original_put
        model = fake_modules.Model()
        streamer = fake_modules.TextStreamer()
        assert model.generate([[1, 2]], streamer=streamer) is model.result
        assert streamer.received == [[[1, 2]], [[3]], [[4]]]
        pipe = fake_modules.TextGenerationPipeline()
        assert pipe("hello") is pipe.result
        assert "requires core privacy support; inference is unchanged" in caplog.text
    finally:
        instrumentor.uninstrument()
