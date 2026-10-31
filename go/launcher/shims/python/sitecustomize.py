"""traceai's fail-open entry point; do not shadow the application's sitecustomize."""
try:
    import os as _os
    import sys as _sys
    # Different launcher caches can put multiple copies of this shim on sys.path.
    # Bootstrap only once, and remember visited copies before chaining to the next.
    if not getattr(_sys, "_traceai_launcher_bootstrapped", False):
        _sys._traceai_launcher_bootstrapped = True
        try:
            from fi_instrumentation.auto._bootstrap import start as _start
            _start()
        except ImportError:
            if _os.environ.get("TRACEAI_DEBUG", "").lower() in ("1", "true", "yes"):
                if not getattr(_sys, "_traceai_missing_reported", False):
                    _sys._traceai_missing_reported = True
                    # Interpreter paths can contain credentials. Keep this
                    # launcher-owned diagnostic value-free, not application output.
                    print("traceai: Python lacks fi-instrumentation-otel; continuing without tracing", file=_sys.stderr)
        except BaseException:
            pass
except BaseException:
    pass

_traceai_next = None
try:
    import importlib.machinery as _machinery
    import importlib.util as _util
    _seen = getattr(_sys, "_traceai_launcher_site_paths", None)
    if _seen is None:
        _seen = _sys._traceai_launcher_site_paths = set()
    _seen.add(_os.path.realpath(_os.path.dirname(__file__)))
    _paths = [p for p in _sys.path if _os.path.realpath(p or _os.getcwd()) not in _seen]
except BaseException:
    _paths = None

if _paths is not None:
    # Follow Python's actual finder order, including editable-install and virtual
    # modules. Filter only our own path entries; do not mutate the app's sys.path.
    # Application finder errors have the same semantics as a native import.
    for _finder in list(_sys.meta_path):
        if _finder is _machinery.PathFinder:
            _spec = _machinery.PathFinder.find_spec("sitecustomize", _paths)
        elif hasattr(_finder, "find_spec"):
            _spec = _finder.find_spec("sitecustomize", None)
        else:
            _loader = _finder.find_module("sitecustomize", None)
            _spec = _util.spec_from_loader("sitecustomize", _loader) if _loader else None
        if _spec is not None and _spec.loader is not None:
            _origin = getattr(_spec, "origin", None)
            if _origin and _os.path.realpath(_os.path.dirname(_origin)) in _seen:
                continue
            _traceai_next = _spec
            break

if _traceai_next is not None:
    # Only instrumentation/discovery errors fail open. Application startup errors
    # must reach Python's normal site machinery (including SystemExit/interrupts).
    # Keep the application's module, so its own import and later imports see it.
    _module = _util.module_from_spec(_traceai_next)
    _sys.modules["sitecustomize"] = _module
    _traceai_next.loader.exec_module(_module)
