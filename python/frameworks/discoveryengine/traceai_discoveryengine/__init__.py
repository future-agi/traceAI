"""OpenTelemetry instrumentation for Google Discovery Engine (google-cloud-discoveryengine).

Wraps v1 search, answers (including streaming), conversation turns, ranking
and grounding, for synchronous and asynchronous clients. Administrative RPCs
and the unversioned v1beta alias are intentionally not patched.
"""

import logging
from importlib import import_module
from typing import Any, Collection, Dict, Tuple

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_discoveryengine._wrappers import (
    ANSWER_QUERY,
    CHECK_GROUNDING,
    CONVERSE_CONVERSATION,
    RANK,
    SEARCH,
    SEARCH_LITE,
    STREAM_ANSWER_QUERY,
    AsyncOperationWrapper,
    OperationWrapper,
    Options,
    _State,
)
from traceai_discoveryengine.package import _instruments
from traceai_discoveryengine.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_SERVICES = "google.cloud.discoveryengine_v1.services."
# (module, class, is_async, operations)
_TARGETS = (
    (_SERVICES + "search_service.client", "SearchServiceClient", False, (SEARCH, SEARCH_LITE)),
    (
        _SERVICES + "search_service.async_client",
        "SearchServiceAsyncClient",
        True,
        (SEARCH, SEARCH_LITE),
    ),
    (
        _SERVICES + "conversational_search_service.client",
        "ConversationalSearchServiceClient",
        False,
        (ANSWER_QUERY, STREAM_ANSWER_QUERY, CONVERSE_CONVERSATION),
    ),
    (
        _SERVICES + "conversational_search_service.async_client",
        "ConversationalSearchServiceAsyncClient",
        True,
        (ANSWER_QUERY, STREAM_ANSWER_QUERY, CONVERSE_CONVERSATION),
    ),
    (_SERVICES + "rank_service.client", "RankServiceClient", False, (RANK,)),
    (_SERVICES + "rank_service.async_client", "RankServiceAsyncClient", True, (RANK,)),
    (
        _SERVICES + "grounded_generation_service.client",
        "GroundedGenerationServiceClient",
        False,
        (CHECK_GROUNDING,),
    ),
    (
        _SERVICES + "grounded_generation_service.async_client",
        "GroundedGenerationServiceAsyncClient",
        True,
        (CHECK_GROUNDING,),
    ),
)


class DiscoveryEngineInstrumentor(BaseInstrumentor):  # type: ignore[misc]
    """Instrument Discovery Engine v1 semantic client methods.

    ``instrument()`` accepts ``tracer_provider`` and ``config`` (a
    ``fi_instrumentation.TraceConfig``, read from the ``FI_HIDE_*`` and
    ``FI_PII_REDACTION`` environment variables when omitted). The query is
    recorded by default, like the input of every traceAI instrumentor, and
    ``hide_inputs`` / ``FI_HIDE_INPUTS`` hides it everywhere, including
    server error text. Spans come from an ``FITracer``, so ``using_session`` /
    ``using_user`` / ``using_metadata`` / ``using_tags`` / ``using_attributes``
    context attributes are stamped on them. ``capture_content=True`` opts in
    to bounded response content; either ``hide_inputs`` or ``hide_outputs``
    overrides it. The default is counts and states only, without output text.
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        config = kwargs.get("config")
        if config is None:
            config = TraceConfig()
        elif not isinstance(config, TraceConfig):
            raise TypeError(
                "config must be a fi_instrumentation.TraceConfig, got {0}".format(
                    type(config).__name__
                )
            )
        if "capture_query" in kwargs:
            # Removed before release: the query follows TraceConfig now.
            logger.warning(
                "capture_query is ignored: the query is recorded unless "
                "TraceConfig(hide_inputs=True) or FI_HIDE_INPUTS=true is set"
            )
        capture_content = kwargs.get("capture_content", False)
        if not isinstance(capture_content, bool):
            raise TypeError("capture_content must be a bool")
        options = Options(
            capture_content=capture_content,
            hide_inputs=bool(config.hide_inputs),
            hide_outputs=bool(config.hide_outputs),
            pii_redaction=bool(config.pii_redaction),
        )
        tracer_provider = kwargs.get("tracer_provider") or trace_api.get_tracer_provider()
        # FITracer applies the config's masking and PII redaction to every
        # attribute and stamps the using_* context attributes.
        tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, tracer_provider), config=config
        )

        self._originals: Dict[Tuple[type, str], Any] = {}
        self._state = _State()
        for module_name, class_name, is_async, operations in _TARGETS:
            try:
                client = getattr(import_module(module_name), class_name, None)
            except ImportError:
                client = None
            for operation in operations:
                if client is None or operation not in vars(client):
                    logger.warning(
                        "Discovery Engine instrumentation skipped %s.%s: not defined on the class",
                        class_name,
                        operation,
                    )
                    continue
                wrapper_type = AsyncOperationWrapper if is_async else OperationWrapper
                wrapper = wrapper_type(tracer, operation, options, self._state)
                self._originals[(client, operation)] = vars(client)[operation]
                wrap_function_wrapper(client, operation, wrapper)
        logger.debug("Discovery Engine instrumentation enabled")

    def _uninstrument(self, **kwargs: Any) -> None:
        state = getattr(self, "_state", None)
        if state is not None:
            # Bound methods read while instrumented stop tracing.
            state.enabled = False
        for (client, operation), original in getattr(self, "_originals", {}).items():
            setattr(client, operation, original)
        self._originals = {}
        logger.debug("Discovery Engine instrumentation disabled")


__all__ = ["DiscoveryEngineInstrumentor", "__version__"]
