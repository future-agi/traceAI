"""Capture request-local credentials before the SDK discards its HTTP envelope."""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional, Tuple

from traceai_openai._privacy import request_credentials
from traceai_openai._with_span import _WithSpan

logger = logging.getLogger(__name__)
_active_request: ContextVar[Optional[Tuple[Any, _WithSpan]]] = ContextVar(
    "traceai_openai_request", default=None
)


@contextmanager
def capture_request_credentials(client: Any, span: _WithSpan) -> Iterator[None]:
    token = _active_request.set((client, span))
    try:
        yield
    finally:
        _active_request.reset(token)


def capture_response_credentials(
    wrapped: Any, instance: Any, args: Any, kwargs: Any
) -> Any:
    # Both SDK bounds pass response by keyword to _process_response, before
    # parsing removes the request. ContextVars isolate concurrent calls without
    # modifying shared clients/transports or invoking a token provider again.
    active = _active_request.get()
    if active is not None and active[0] is instance:
        try:
            response = kwargs.get("response")
            active[1].add_secrets(
                request_credentials(None, getattr(response, "request", None), {})
            )
        except Exception:
            logger.warning("Failed to capture request credentials")
    return wrapped(*args, **kwargs)
