"""A LangSmith-traced app whose runs also reach Future AGI as OpenTelemetry spans.

With OTEL mode on, the LangSmith SDK turns every run into an OpenTelemetry
span. ``init_tracing()`` installs the tracer provider those spans go to: the
Future AGI project on the resource, and an OTLP/HTTP exporter that reads its
endpoint and headers from the standard ``OTEL_EXPORTER_OTLP_*`` variables.
That is the whole integration; the rest is an ordinary ``@traceable`` app.

Usage: python app.py "What is the refund window?"
"""

from __future__ import annotations

import os
import sys

import langsmith
from langsmith import traceable
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

MODEL = os.environ.get("APP_MODEL", "gpt-4o-mini")
POLICY = "Refunds are accepted within 30 days of purchase."


def init_tracing() -> TracerProvider:
    """Install the global provider that LangSmith's OTEL exporter writes into.

    Call it before the first traced call. LangSmith adopts a global provider
    that already exists; otherwise it installs its own, whose resource has no
    ``project_name``, and fi-collector rejects that.
    """
    provider = TracerProvider(
        resource=Resource.create(
            {
                "project_name": os.environ["FI_PROJECT_NAME"],
                "project_type": "observe",
            }
        )
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    return provider


@traceable(run_type="tool", name="lookup_policy")
def lookup_policy(topic: str) -> str:
    return POLICY


@traceable(
    run_type="llm",
    name="chat_model",
    metadata={"ls_provider": "openai", "ls_model_name": MODEL},
)
def chat_model(messages: list[dict]) -> dict:
    """Stand-in for your model call, returning what a chat model run returns.

    LangSmith reads the model name from ``ls_model_name`` and the token
    counts from ``usage_metadata`` in the outputs.
    """
    answer = "Refunds are accepted for 30 days."
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }
        ],
        "usage_metadata": {"input_tokens": 21, "output_tokens": 9, "total_tokens": 30},
    }


@traceable(run_type="chain", name="support_request")
def support_request(question: str) -> str:
    policy = lookup_policy("refunds")
    response = chat_model(
        [
            {"role": "system", "content": "Answer from this policy: " + policy},
            {"role": "user", "content": question},
        ]
    )
    return response["choices"][0]["message"]["content"]


def main() -> None:
    provider = init_tracing()
    client = langsmith.Client()  # created after init_tracing(), so it adopts the provider
    langsmith.configure(client=client)

    question = sys.argv[1] if len(sys.argv) > 1 else "What is the refund window?"
    print(support_request(question))

    # A short script exits before the background queues drain: hand LangSmith's
    # queued runs to the provider, then export them.
    client.flush()
    provider.shutdown()


if __name__ == "__main__":
    main()
