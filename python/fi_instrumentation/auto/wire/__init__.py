"""Wire layer: traces AI API calls at the HTTP client, for any SDK or framework.

Recognizes the protocols in ``wire-protocols.json`` (OpenAI-compatible APIs, Anthropic,
Gemini/Vertex, Cohere, Bedrock, Ollama, rerank, and speech APIs such as OpenAI audio,
Deepgram, ElevenLabs, AssemblyAI and Cartesia) on httpx, httpx2, requests and aiohttp, and records
each call as a span unless a library-layer span already covers it.
"""

import importlib.util
import logging
import sys
import threading
from typing import Any, Dict, Iterable, Optional

import wrapt

from fi_instrumentation.auto.wire._capture import Recorder
from fi_instrumentation.auto.wire._core import DEFAULT_MAX_BODY_BYTES
from fi_instrumentation.auto.wire._transports import TRANSPORTS

logger = logging.getLogger(__name__)

__all__ = ["enable", "disable", "transport_statuses", "TRANSPORTS"]

_lock = threading.Lock()
_recorder: Optional[Recorder] = None
_patched: Dict[str, Any] = {}
_hooked: set = set()
_errors: Dict[str, str] = {}
_selected: set = set()


def _patch(name: str, module: Any) -> None:
    with _lock:
        recorder = _recorder
        if recorder is None or name not in _selected or name in _patched:
            return
        try:
            TRANSPORTS[name][0](recorder, module)
            _patched[name] = module
            _errors.pop(name, None)
        except Exception as error:  # an incompatible client version must not break the app
            _errors[name] = f"{type(error).__name__}: {error}"
            logger.warning("traceAI: could not hook %s for wire capture: %s", name, error)


def enable(
    tracer: Any,
    *,
    transports: Optional[Iterable[str]] = None,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    credentials: Iterable[str] = (),
) -> Dict[str, str]:
    """Hook the selected HTTP clients now if imported, otherwise when they are first imported."""
    global _recorder
    from fi_instrumentation._secrets import register_secret, seed_environment

    seed_environment()
    for value in credentials:
        register_secret(value)
    names = list(transports) if transports is not None else list(TRANSPORTS)
    with _lock:
        _selected.update(names)
        if _recorder is None:
            _recorder = Recorder(tracer, max_body_bytes)
    for name in names:
        if name not in TRANSPORTS:
            continue
        if name in _hooked:
            if name in sys.modules:
                _patch(name, sys.modules[name])
            continue
        _hooked.add(name)
        wrapt.register_post_import_hook(lambda module, name=name: _patch(name, module), name)
    return transport_statuses(names)


def disable() -> None:
    """Remove the hooks. Clients imported later are left alone."""
    global _recorder
    with _lock:
        _selected.clear()
        if _recorder is not None:
            _recorder.enabled = False
        for name, module in list(_patched.items()):
            try:
                TRANSPORTS[name][1](module)
            except Exception:
                logger.debug("traceAI: unhooking %s failed", name, exc_info=True)
            _patched.pop(name, None)
        _recorder = None


def transport_statuses(names: Optional[Iterable[str]] = None) -> Dict[str, str]:
    """``instrumented``, ``pending`` (not imported yet), ``not_installed`` or ``failed: ...``."""
    statuses = {}
    for name in names if names is not None else TRANSPORTS:
        if name in _errors:
            statuses[name] = f"failed: {_errors[name]}"
        elif name in _patched:
            statuses[name] = "instrumented"
        elif name in sys.modules:
            statuses[name] = "pending"
        elif importlib.util.find_spec(name) is None:
            statuses[name] = "not_installed"
        else:
            statuses[name] = "pending"
    return statuses
