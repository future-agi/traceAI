"""CrewAI-local batch projection before core's per-attribute privacy policy."""

import wrapt
from fi_instrumentation import FITracer
from opentelemetry.trace import Link, SpanKind

from ._privacy import _redact_header_records


def _project_attributes(attributes):
    # Keep selector/value siblings together before core's scalar masking; leave
    # unrelated values and core's lazy evaluation semantics unchanged.
    return None if attributes is None else _redact_header_records(dict(attributes))


class _CrewAISpan(wrapt.ObjectProxy):
    """Retain core span lifecycle/error policy; project only attribute batches.

    No global FiSpan/SDK patches, shared selector state, or application mutation.
    The complete batch is projected before core masks each scalar, regardless
    of selector/value ordering in the mapping.
    """

    def __enter__(self):
        self.__wrapped__.__enter__()
        return self

    def __exit__(self, *args):
        return self.__wrapped__.__exit__(*args)

    def set_attributes(self, attributes):
        self.__wrapped__.set_attributes(_project_attributes(attributes))

    def add_event(self, name, attributes=None, timestamp=None):
        self.__wrapped__.add_event(name, _project_attributes(attributes), timestamp)

    def record_exception(
        self, exception, attributes=None, timestamp=None, escaped=False
    ):
        self.__wrapped__.record_exception(
            exception, _project_attributes(attributes), timestamp, escaped
        )

    def add_link(self, context, attributes=None):
        self.__wrapped__.add_link(context, _project_attributes(attributes))


class _CrewAITracer(FITracer):
    def start_span(
        self,
        name,
        context=None,
        kind=SpanKind.INTERNAL,
        attributes=None,
        links=(),
        start_time=None,
        record_exception=True,
        set_status_on_exception=True,
        *,
        fi_span_kind=None,
    ):
        # Creation attributes/links are projected before samplers/processors can
        # observe them, not repaired after the raw span has already started.
        projected_links = (
            None
            if links is None
            else [
                Link(link.context, _project_attributes(link.attributes))
                for link in links
            ]
        )
        span = super().start_span(
            name=name,
            context=context,
            kind=kind,
            attributes=_project_attributes(attributes),
            links=projected_links,
            start_time=start_time,
            record_exception=record_exception,
            set_status_on_exception=set_status_on_exception,
            fi_span_kind=fi_span_kind,
        )
        return _CrewAISpan(span)
