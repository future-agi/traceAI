"""Send OpenLLMetry (Traceloop SDK) traces to Future AGI.

Recipe example, not a package. The Traceloop SDK already exports OTLP/HTTP;
this file only calls ``Traceloop.init`` with the resource attributes
fi-collector needs and wraps one model call in Traceloop's decorators.
Everything else (endpoint, auth headers, metrics, content) comes from the
environment variables listed in README.md.

    python src/app.py "What is the refund window?"
"""

from __future__ import annotations

import os
import sys

from openai import OpenAI
from traceloop.sdk import Traceloop
from traceloop.sdk.decorators import agent, task, tool, workflow

APP_NAME = "openllmetry-recipe"  # exported as the service.name resource attribute
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

_POLICIES = {
    "refund": "Refunds are accepted within 30 days of purchase.",
    "shipping": "Orders ship within 2 business days.",
}


def init_tracing() -> None:
    """Start Traceloop's exporter. Call once per process, before any model call.

    TRACELOOP_BASE_URL, TRACELOOP_HEADERS, TRACELOOP_METRICS_ENABLED and
    TRACELOOP_TRACE_CONTENT are read by the SDK itself (see README.md).
    """
    if os.environ.get("TRACELOOP_API_KEY"):
        # That key is for Traceloop's own host. Without TRACELOOP_HEADERS the
        # SDK sends it as a bearer token with every export; with them it is
        # not sent with spans, but the SDK still hands it to its image
        # uploader, which sends it as a bearer token to TRACELOOP_BASE_URL
        # when content capture is on and a prompt carries a base64 image.
        raise SystemExit("Unset TRACELOOP_API_KEY when exporting to Future AGI.")
    Traceloop.init(
        app_name=APP_NAME,
        # fi-collector rejects (HTTP 400) a batch whose resource has no
        # project_name, and creates the project on first use.
        resource_attributes={
            "project_name": os.environ["FI_PROJECT_NAME"],
            "project_type": "observe",
        },
        # traceloop-sdk 0.62.4 accepts this flag and has no telemetry code
        # behind it; it is passed so nothing depends on that.
        telemetry_enabled=False,
    )


@tool(name="lookup_policy")
def lookup_policy(question: str) -> str:
    topic = "refund" if "refund" in question.lower() else "shipping"
    return _POLICIES[topic]


@agent(name="support_agent")
def support_agent(question: str) -> str:
    facts = lookup_policy(question)
    # OpenAI() reads OPENAI_API_KEY and OPENAI_BASE_URL.
    response = OpenAI().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "Answer using only these facts: " + facts},
            {"role": "user", "content": question},
        ],
    )
    return response.choices[0].message.content or ""


@task(name="normalize_question")
def normalize_question(question: str) -> str:
    return " ".join(question.split())


@workflow(name="support_request")
def support_request(question: str) -> str:
    # Model calls made inside a @workflow carry traceloop.workflow.name, the
    # traceloop.* marker Future AGI uses to recognise OpenLLMetry spans.
    return support_agent(normalize_question(question))


def main(argv: list[str]) -> int:
    init_tracing()
    question = argv[1] if len(argv) > 1 else "What is the refund window?"
    print(support_request(question))
    # Traceloop registers an atexit flush, so a short script needs no more.
    # A long-lived server keeps the default batch processor running.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
