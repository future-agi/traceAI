"""Send MLflow traces to Future AGI over OTLP.

Recipe example, not a package. MLflow already exports its own spans over
OTLP when ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` is set before the first
span; this file only checks that environment and creates two nested spans
with ``mlflow.start_span``. The endpoint, protocol, Future AGI keys and
project come from the environment variables listed in README.md. With those
variables (OTLP only, no Databricks tracking URI, ``MLFLOW_MODEL_CATALOG_URI``
empty) MLflow sends the spans to the OTLP endpoint and contacts nothing else
(tested); ``check_environment()`` refuses the settings that would make it
call Databricks or send the traces elsewhere. The model client is a
stand-in, so there is no model call.

    python src/app.py "What is the refund window?"
    python src/app.py --drop-content "What is the refund window?"
"""

from __future__ import annotations

import os
import sys
from urllib.parse import unquote, urlparse

import mlflow
from mlflow.entities import LiveSpan, SpanType

MODEL = "gpt-4o-mini"
PROVIDER = "openai"
REDACTED = "[REDACTED]"

_POLICY = "Refunds are accepted within 30 days of purchase."


def _dual_export() -> bool:
    # Parsed as MLflow parses its boolean variables.
    return os.environ.get("MLFLOW_TRACE_ENABLE_OTLP_DUAL_EXPORT", "").lower() in ("true", "1")


def check_environment() -> None:
    """Exit with a message on the settings that make MLflow 3.16.1 send nothing
    to Future AGI, send traces or keys elsewhere, or call Databricks.

    None of these raises an exception in the app (see README.md).
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")
    if endpoint.endswith("/") and endpoint.rstrip("/").endswith("/v1/traces"):
        # MLflow posts to this URL as is, and fi-collector serves only the
        # exact paths, so the slash gets a 404.
        raise SystemExit("Remove the trailing slash from OTEL_EXPORTER_OTLP_TRACES_ENDPOINT.")
    if not endpoint.endswith("/v1/traces"):
        # MLflow uses this URL as is; it does not append /v1/traces. When the
        # variable is unset, MLflow writes traces to a local ./mlflow.db.
        raise SystemExit(
            "Set OTEL_EXPORTER_OTLP_TRACES_ENDPOINT to the full traces URL, "
            "for example https://<collector>/tracer/v1/traces."
        )
    # MLflow reads the TRACES protocol, then the general one. Its default is
    # gRPC, and it skips export if that exporter is not installed.
    protocol = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL") or os.environ.get(
        "OTEL_EXPORTER_OTLP_PROTOCOL", "grpc"
    )
    if protocol != "http/protobuf":
        raise SystemExit("Set OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf.")
    for name in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"):
        if os.environ.get(name):
            # Either one also turns on MLflow's OTLP metrics, which fi-collector
            # does not accept; with the default gRPC metrics protocol and no
            # gRPC exporter installed, MLflow then drops every span.
            raise SystemExit("Unset {0}; set only the TRACES variables.".format(name))
    if os.environ.get("OTEL_EXPORTER_OTLP_HEADERS"):
        # The exporter sends these instead when the TRACES headers are empty,
        # so a key meant for another destination could reach Future AGI.
        raise SystemExit(
            "Unset OTEL_EXPORTER_OTLP_HEADERS; put the Future AGI keys in "
            "OTEL_EXPORTER_OTLP_TRACES_HEADERS."
        )
    # Header names as the exporter parses them. Values are never printed.
    headers = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "")
    names = {unquote(item.split("=", 1)[0]).strip().lower() for item in headers.split(",") if "=" in item}
    missing = [name for name in ("x-api-key", "x-secret-key") if name not in names]
    if missing:
        raise SystemExit(
            "Set OTEL_EXPORTER_OTLP_TRACES_HEADERS to x-api-key=<key>,x-secret-key=<secret> "
            "(missing: {0}).".format(", ".join(missing))
        )
    if os.environ.get("MLFLOW_TRACING_DESTINATION") and not _dual_export():
        # MLflow sends the traces to that destination (an experiment's store,
        # or Databricks) instead of the OTLP endpoint, and says nothing.
        raise SystemExit("Unset MLFLOW_TRACING_DESTINATION; it replaces the OTLP export.")
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", "")
    if (tracking_uri == "databricks" or urlparse(tracking_uri).scheme == "databricks") and not _dual_export():
        # MLflow then reads the experiment from the workspace on the first
        # span, fetches its model catalog to price model spans, and sends the
        # traces to Unity Catalog instead of OTLP if the experiment is linked
        # to it.
        raise SystemExit(
            "Unset MLFLOW_TRACKING_URI for this recipe: with a Databricks tracking URI "
            "MLflow calls Databricks and can replace the OTLP export."
        )
    resource = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
    items = [item.split("=", 1) for item in resource.split(",")] if resource else []
    if any(len(item) != 2 for item in items):
        # MLflow re-parses this variable and, if one item has no "=" (a
        # trailing comma is enough), drops every attribute in it.
        raise SystemExit("Write OTEL_RESOURCE_ATTRIBUTES as key=value pairs joined by commas.")
    if not dict((key.strip(), value.strip()) for key, value in items).get("project_name"):
        # fi-collector rejects (HTTP 400) a batch whose resource has no
        # project_name, and creates the project on first use.
        raise SystemExit("Add project_name=<your project> to OTEL_RESOURCE_ATTRIBUTES.")


def drop_content(span: LiveSpan) -> None:
    """Replace a span's inputs and outputs before it is exported.

    MLflow has no setting that keeps tracing on and content off; a span
    processor registered with ``mlflow.tracing.configure`` is its own hook.
    """
    if span.inputs is not None:
        span.set_inputs(REDACTED)
    if span.outputs is not None:
        span.set_outputs(REDACTED)


def call_model(messages: list[dict[str, str]]) -> tuple[str, dict[str, int]]:
    """Stand-in for your model client: a canned answer and its token counts."""
    return "Refunds are accepted for 30 days.", {"input_tokens": 12, "output_tokens": 7}


def answer(question: str) -> str:
    with mlflow.start_span(name="answer_question", span_type=SpanType.CHAIN) as root:
        root.set_inputs({"question": question})
        with mlflow.start_span(name="chat_model", span_type=SpanType.CHAT_MODEL) as llm:
            messages = [
                {"role": "system", "content": "Answer from this policy: " + _POLICY},
                {"role": "user", "content": question},
            ]
            llm.set_inputs({"model": MODEL, "messages": messages})
            reply, usage = call_model(messages)
            llm.set_outputs({"choices": [{"message": {"role": "assistant", "content": reply}}]})
            # The OTel GenAI keys fi-collector reads. MLflow JSON-encodes every
            # attribute value: the token counts still parse, but the model
            # arrives with its quotes, so fi-collector prices nothing (cost 0;
            # see README.md).
            llm.set_attributes(
                {
                    "gen_ai.request.model": MODEL,
                    "gen_ai.provider.name": PROVIDER,
                    "gen_ai.usage.input_tokens": usage["input_tokens"],
                    "gen_ai.usage.output_tokens": usage["output_tokens"],
                }
            )
            # The keys MLflow's own autolog sets on a model call. With
            # MLFLOW_ENABLE_OTEL_GENAI_SEMCONV=true, MLflow exports them as
            # plain gen_ai.* values instead.
            llm.set_attributes(
                {
                    "mlflow.llm.model": MODEL,
                    "mlflow.llm.provider": PROVIDER,
                    "mlflow.chat.tokenUsage": {
                        **usage,
                        "total_tokens": usage["input_tokens"] + usage["output_tokens"],
                    },
                }
            )
        root.set_outputs({"answer": reply})
    return reply


def main(argv: list[str]) -> int:
    args = argv[1:]
    check_environment()
    if args[:1] == ["--drop-content"]:
        mlflow.tracing.configure(span_processors=[drop_content])
        args = args[1:]
    question = args[0] if args else "What is the refund window?"
    print(answer(question))
    # MLflow's OTLP exporter runs in an OpenTelemetry BatchSpanProcessor, and
    # the OpenTelemetry SDK flushes it at exit, so a short script needs no more.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
