"""Framework retrieval spans; vector-store instrumentation remains independent."""

from typing import Any

from openinference.instrumentation.agno.utils import _bind_arguments
from opentelemetry import context, trace


class _KnowledgeWrapper:
    def __init__(self, tracer: trace.Tracer, *, hide_documents: bool) -> None:
        self._tracer = tracer
        self._hide_documents = hide_documents

    @staticmethod
    def _attributes(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> dict:
        arguments = _bind_arguments(wrapped, *args, **kwargs)
        attributes = {"openinference.span.kind": "RETRIEVER"}
        query = arguments.get("query")
        if isinstance(query, str):
            attributes["input.value"] = query
        if name := getattr(instance, "name", None):
            attributes["gen_ai.data_source.id"] = name
        return attributes

    def _output(self, span: Any, documents: Any) -> None:
        span.set_attribute("agno.knowledge.documents_count", len(documents))
        if self._hide_documents:
            return
        for index, document in enumerate(documents):
            # Do not serialize the Document model: it can include embeddings,
            # provider configuration and arbitrary application metadata.
            for field in ("id", "content"):
                value = getattr(document, field, None)
                if isinstance(value, str):
                    span.set_attribute(
                        f"retrieval.documents.{index}.document.{field}", value
                    )

    def run(self, wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        if context.get_value(context._SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)
        with self._tracer.start_as_current_span(
            "Knowledge.search",
            attributes=self._attributes(wrapped, instance, args, kwargs),
        ) as span:
            documents = wrapped(*args, **kwargs)
            self._output(span, documents)
            span.set_status(trace.StatusCode.OK)
            return documents

    async def arun(self, wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        if context.get_value(context._SUPPRESS_INSTRUMENTATION_KEY):
            return await wrapped(*args, **kwargs)
        # use_span only records Exception, not task cancellation (BaseException).
        with self._tracer.start_as_current_span(
            "Knowledge.asearch",
            attributes=self._attributes(wrapped, instance, args, kwargs),
        ) as span:
            try:
                documents = await wrapped(*args, **kwargs)
            except BaseException as error:
                span.set_status(
                    trace.StatusCode.ERROR, str(error) or type(error).__name__
                )
                raise
            self._output(span, documents)
            span.set_status(trace.StatusCode.OK)
            return documents
