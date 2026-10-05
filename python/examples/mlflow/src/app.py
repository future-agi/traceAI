"""Send MLflow traces to Future AGI over OTLP.

Recipe example, not a package. MLflow already exports its own spans over
OTLP when ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` is set before the first
span; this file only checks that environment and creates two nested spans
with ``mlflow.start_span``. The endpoint, protocol, Future AGI keys and
project come from the environment variables listed in README.md. Nothing
here talks to an MLflow tracking server, Databricks or a model provider.

    python src/app.py "What is the refund window?"
    python src/app.py --drop-content "What is the refund window?"
"""

from __future__ import annotations

import os
import sys

import mlflow
from mlflow.entities import LiveSpan, SpanType

MODEL = "gpt-4o-mini"
PROVIDER = "openai"
REDACTED = "[REDACTED]"

_POLICY = "Refunds are accepted within 30 days of purchase."


def check_environment() -> None:
    """Exit with a message on the settings that make MLflow 3.16.1 export nothing.

    Each of these fails without an exception in the app (see README.md).
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")
    if not endpoint.rstrip("/").endswith("/v1/traces"):
        # MLflow uses this URL as is; it does not append /v1/traces. When the
        # variable is unset, MLflow writes traces to a local ./mlflow.db.
        raise SystemExit(
            "Set OTEL_EXPORTER_OTLP_TRACES_ENDPOINT to the full traces URL, "
            "for example https://<collector>/tracer/v1/traces."
        )
    if os.environ.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL") != "http/protobuf":
        # MLflow's default is gRPC, and it skips export if that exporter is
        # not installed.
        raise SystemExit("Set OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf.")
    for name in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"):
        if os.environ.get(name):
            # Either one also turns on MLflow's OTLP metrics, which fi-collector
            # does not accept; with the default gRPC metrics protocol and no
            # gRPC exporter installed, MLflow then drops every span.
            raise SystemExit("Unset {0}; set only the TRACES variables.".format(name))
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
            # attribute value, so these arrive as strings (see README.md).
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
