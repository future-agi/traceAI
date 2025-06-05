import logging
from contextlib import contextmanager
from itertools import chain
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from fi_instrumentation import get_attributes_from_context, safe_json_dumps
from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer

logger = logging.getLogger(__name__)

# Constants for Guardrail span attributes
GUARDRAIL_TYPE = "guardrail.type"
GUARDRAIL_RULES = "guardrail.rules"
GUARDRAIL_STATUS = "guardrail.status"
GUARDRAIL_FAILED_RULE = "guardrail.failed_rule"
GUARDRAIL_REASONS = "guardrail.reasons"
GUARDRAIL_TIME_TAKEN = "guardrail.time_taken"
GUARDRAIL_COMPLETED_RULES = "guardrail.completed_rules"
GUARDRAIL_UNCOMPLETED_RULES = "guardrail.uncompleted_rules"
GUARDRAIL_ACTION = "guardrail.action"
GUARDRAIL_REASON_FLAG = "guardrail.reason_flag"


def _get_raw_input(inputs: Any) -> Iterator[Tuple[str, Any]]:
    try:
        yield SpanAttributes.RAW_INPUT, safe_json_dumps(inputs)
    except Exception as e:
        logger.warning(f"Failed to serialize raw guardrail inputs: {e}", exc_info=True)


def _get_raw_output(result: Dict[str, Any]) -> Iterator[Tuple[str, Any]]:
    try:
        yield SpanAttributes.RAW_OUTPUT, safe_json_dumps(result)
    except Exception as e:
        logger.warning(f"Failed to serialize raw guardrail output: {e}", exc_info=True)


def _get_protect_input(inputs: Any) -> Iterator[Tuple[str, Any]]:
    try:
        yield SpanAttributes.INPUT_VALUE, safe_json_dumps(inputs)
        yield SpanAttributes.INPUT_MIME_TYPE, "text/plain"
    except Exception as e:
        logger.warning(f"Failed to serialize guardrail inputs: {e}", exc_info=True)


def _get_protect_rules(rules: List[Dict[str, Any]]) -> Iterator[Tuple[str, Any]]:
    try:
        yield GUARDRAIL_RULES, safe_json_dumps(rules)
    except Exception as e:
        logger.warning(f"Failed to serialize guardrail rules: {e}", exc_info=True)


def _get_protect_output(result: Dict[str, Any]) -> Iterator[Tuple[str, Any]]:
    try:
        yield SpanAttributes.OUTPUT_VALUE, safe_json_dumps(result)
        yield SpanAttributes.OUTPUT_MIME_TYPE, "application/json"

        if status := result.get("status"):
            yield GUARDRAIL_STATUS, status
        if failed_rule := result.get("failed_rule"):
            yield GUARDRAIL_FAILED_RULE, failed_rule
        if reasons := result.get("reasons"):
            yield GUARDRAIL_REASONS, safe_json_dumps(reasons)
        if time_taken := result.get("time_taken"):
            yield GUARDRAIL_TIME_TAKEN, time_taken
        if completed := result.get("completed_rules"):
            yield GUARDRAIL_COMPLETED_RULES, safe_json_dumps(completed)
        if uncompleted := result.get("uncompleted_rules"):
            yield GUARDRAIL_UNCOMPLETED_RULES, safe_json_dumps(uncompleted)
    except Exception as e:
        logger.warning(f"Failed to serialize guardrail output: {e}", exc_info=True)


class GuardrailProtectWrapper:
    """Wraps the fi.evals.Protect.protect method to create a Guardrail span."""

    def __init__(self, tracer: Tracer):
        self._tracer = tracer

    @contextmanager
    def _start_as_current_span(
        self, span_name: str, attributes: Optional[Mapping[str, Any]] = None
    ) -> Iterator[trace_api.Span]:
        """Creates and manages a span with proper context handling."""
        try:
            if hasattr(self._tracer, "_tracer"):
                span = self._tracer._tracer.start_span(
                    name=span_name,
                    kind=SpanKind.INTERNAL,
                    attributes=attributes,
                )
            else:
                span = self._tracer.start_span(
                    name=span_name,
                    kind=SpanKind.INTERNAL,
                    attributes=attributes,
                )
        except Exception as e:
            logger.error(f"Error creating span: {e}", exc_info=True)
            span = trace_api.INVALID_SPAN

        with trace_api.use_span(
            span,
            end_on_exit=True,
            record_exception=True,
            set_status_on_exception=True,
        ) as current_span:
            yield current_span

    def __call__(
        self,
        wrapped: Any,
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        """Executes the wrapped protect method and creates a span."""
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)

        inputs = kwargs.get("inputs") or (args[0] if args else None)
        protect_rules = kwargs.get("protect_rules") or (
            args[1] if len(args) > 1 else None
        )
        action = kwargs.get("action")
        reason_flag = kwargs.get("reason")

        if not inputs or not protect_rules:
            logger.debug(
                "Guardrail protect called without inputs or rules. Skipping tracing."
            )
            return wrapped(*args, **kwargs)

        with self._start_as_current_span(
            span_name="Guardrail.protect",
            attributes=dict(
                chain(
                    get_attributes_from_context(),
                    [(SpanAttributes.FI_SPAN_KIND, FiSpanKindValues.GUARDRAIL.value)],
                    [(GUARDRAIL_TYPE, "protect")],
                    _get_protect_input(inputs),
                    _get_protect_rules(protect_rules),
                    _get_raw_input(kwargs),
                    ([(GUARDRAIL_ACTION, action)] if action is not None else []),
                    (
                        [(GUARDRAIL_REASON_FLAG, reason_flag)]
                        if reason_flag is not None
                        else []
                    ),
                )
            ),
        ) as span:
            try:
                result = wrapped(*args, **kwargs)

                span.set_attributes(
                    dict(
                        chain(
                            _get_protect_output(result),
                            _get_raw_output(result),
                        )
                    )
                )

                if result.get("status") == "passed":
                    span.set_status(Status(StatusCode.OK))
                else:
                    failed_rule = result.get("failed_rule", "unknown")
                    span.set_status(
                        Status(
                            StatusCode.ERROR, f"Guardrail check failed: {failed_rule}"
                        )
                    )

                return result

            except Exception as e:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))
                logger.error(f"Exception during protect call: {e}", exc_info=True)
                raise
