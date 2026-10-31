"""Session propagation without inventing workflow spans or IDs."""

import logging

from opentelemetry import context, trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import Span, SpanProcessor
from temporalio import activity, workflow

_LOGGER = logging.getLogger(__name__)


class WorkflowIdAsSession(SpanProcessor):
    """Use the native workflow ID unless the application supplies a session.

    Activity runtime context also covers SDK versions that omit workflow IDs on
    RunActivity. Only local parent/context user IDs propagate: no new baggage or
    workflow headers are added, and identities never cross a workflow boundary
    implicitly.
    """

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        # OTel 1.39's default SpanProcessor method returns None, which aborts
        # the provider's flush before it reaches the actual batch processor.
        return True

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        try:
            attrs = span.attributes or {}
            parent = (
                getattr(trace.get_current_span(parent_context), "attributes", None)
                or {}
            )
            if not attrs.get("session.id"):
                session = context.get_value("session.id", parent_context)
                if not session:
                    session = attrs.get("temporalWorkflowID")
                if not session and activity.in_activity():
                    session = activity.info().workflow_id
                if not session and workflow.in_workflow():
                    session = workflow.info().workflow_id
                if not session:
                    session = parent.get("session.id")
                if isinstance(session, str) and session:
                    span.set_attribute("session.id", session)
            if not attrs.get("user.id"):
                user = context.get_value("user.id", parent_context) or parent.get(
                    "user.id"
                )
                if isinstance(user, str) and user:
                    span.set_attribute("user.id", user)
        except Exception:  # noqa: BLE001 -- telemetry hooks must not break workflows
            # Do not log exception text/stacks: they may include application content.
            _LOGGER.warning(
                "Temporal session mapping failed; span kept, details redacted"
            )
