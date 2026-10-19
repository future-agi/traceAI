"""Helpers that keep error details (messages, tracebacks, provider error bodies)
out of span data.

Error text is content: provider error bodies quote the prompt back, carry
request and session ids, and Python tracebacks carry local file paths. The
``FiSpan`` wrapper uses these helpers so every instrumentor that goes through
``FITracer`` gets the same treatment.
"""

import re
from typing import Dict, Iterable, Mapping, Optional

from opentelemetry.util.types import AttributeValue

EXCEPTION_EVENT_NAME = "exception"
EXCEPTION_TYPE = "exception.type"
EXCEPTION_MESSAGE = "exception.message"
EXCEPTION_STACKTRACE = "exception.stacktrace"
EXCEPTION_ESCAPED = "exception.escaped"
ERROR_TYPE = "error.type"
ERROR_MESSAGE = "error.message"

# Attribute keys that carry error text. They are dropped when
# ``TraceConfig.hide_error_details`` is on.
ERROR_DETAIL_KEYS = frozenset({EXCEPTION_MESSAGE, EXCEPTION_STACKTRACE, ERROR_MESSAGE})

# Instrumentor-specific keys follow the same shape, e.g. ``autogen.error.message``
# or ``autogen.tool.error_message``.
_ERROR_DETAIL_SUFFIXES = (
    ".exception.message",
    ".exception.stacktrace",
    ".error.message",
    ".error_message",
)

_TRACEBACK_MARKER = "Traceback (most recent call last)"

# A provider error code is a short identifier such as ``model_not_found``,
# ``ValidationException`` or ``NOT_FOUND``. Anything else (free text, JSON,
# a quoted prompt) is not used as ``error.type``.
_ERROR_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:\-]{0,63}$")


def is_error_detail_key(key: str) -> bool:
    return key in ERROR_DETAIL_KEYS or key.endswith(_ERROR_DETAIL_SUFFIXES)


def strip_error_details(
    attributes: Optional[Mapping[str, AttributeValue]],
) -> Optional[Dict[str, AttributeValue]]:
    if attributes is None:
        return None
    return {k: v for k, v in attributes.items() if not is_error_detail_key(k)}


def qualified_exception_type(exception: BaseException) -> str:
    """Same format as the OpenTelemetry SDK's ``exception.type``."""
    cls = type(exception)
    module = cls.__module__
    qualname = cls.__qualname__
    if module and module != "builtins":
        return f"{module}.{qualname}"
    return qualname


def _as_code(value: object) -> Optional[str]:
    if isinstance(value, str) and _ERROR_CODE_RE.match(value):
        return value
    return None


def _provider_error_code(exception: BaseException) -> Optional[str]:
    if type(exception).__module__ == "builtins":
        # e.g. SystemExit.code can be an arbitrary message.
        return None
    # OpenAI-compatible SDKs (openai, groq, together, xai, litellm, ...).
    if code := _as_code(getattr(exception, "code", None)):
        return code
    # botocore ClientError: response["Error"]["Code"].
    response = getattr(exception, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping) and (code := _as_code(error.get("Code"))):
            return code
    # Anthropic: body = {"type": "error", "error": {"type": "not_found_error", ...}}.
    body = getattr(exception, "body", None)
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            for key in ("code", "type"):
                if code := _as_code(error.get(key)):
                    return code
    # google-genai APIError: status = "NOT_FOUND" (``code`` is the HTTP int).
    if code := _as_code(getattr(exception, "status", None)):
        return code
    # OpenAI APIError without a ``code``: type = "invalid_request_error".
    if code := _as_code(getattr(exception, "type", None)):
        return code
    return None


def get_error_type(exception: BaseException) -> str:
    """Value for ``error.type``: the provider error code when the exception
    carries one, otherwise the fully qualified exception class."""
    try:
        if code := _provider_error_code(exception):
            return code
    except Exception:
        pass
    return qualified_exception_type(exception)


def _exception_message(exception: BaseException) -> Optional[str]:
    try:
        message = str(exception)
    except Exception:
        return None
    return message or None


def _cut_traceback(description: str) -> Optional[str]:
    index = description.find(_TRACEBACK_MARKER)
    if index == -1:
        return description
    return description[:index].rstrip() or None


def clean_error_description(
    description: Optional[str],
    exceptions: Iterable[Optional[BaseException]],
) -> Optional[str]:
    """Reduce an ERROR status description to the exception message.

    ``use_span`` and most instrumentors build ``"<Type>: <message>"``; the type
    already lives in ``error.type`` and ``exception.type``, so the description
    keeps only ``str(exc)``. A Python traceback never stays in the description.
    """
    if not description:
        return description
    known = [e for e in exceptions if e is not None]
    for exception in known:
        message = _exception_message(exception)
        if description == message:
            return _cut_traceback(description)
        prefixes = {type(exception).__name__, qualified_exception_type(exception)}
        stripped = description.rstrip()
        if any(stripped == f"{prefix}: {message or ''}".rstrip() for prefix in prefixes):
            return _cut_traceback(message) if message else None
    if _TRACEBACK_MARKER in description:
        if known:
            message = _exception_message(known[0])
            return _cut_traceback(message) if message else None
        return _cut_traceback(description)
    return description
