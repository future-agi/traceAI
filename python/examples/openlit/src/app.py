"""Send OpenLIT traces to Future AGI.

Recipe example, not a package. OpenLIT already exports OTLP/HTTP; this file
only calls ``openlit.init`` with the collector endpoint, the Future AGI keys
and the switches README.md explains, then makes one OpenAI call that
OpenLIT's auto-instrumentation traces. The project comes from the standard
``OTEL_RESOURCE_ATTRIBUTES`` variable (see README.md).

    python src/app.py "What is the refund window?"
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import quote

import openlit
from openai import OpenAI
from opentelemetry.sdk.resources import OTELResourceDetector

APP_NAME = "openlit-recipe"  # exported as the service.name resource attribute
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
# An empty price table. Without pricing_json, openlit.init() downloads one
# from raw.githubusercontent.com on every start.
NO_PRICING = Path(__file__).resolve().with_name("no_pricing.json")

_POLICY = "Refunds are accepted within 30 days of purchase."


def init_tracing() -> None:
    """Start OpenLIT's exporter. Call once per process, before any model call."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        # Without an endpoint OpenLIT prints every span to stdout instead.
        raise SystemExit("Set OTEL_EXPORTER_OTLP_ENDPOINT to the fi-collector origin.")
    api_key = os.environ.get("FI_API_KEY")
    secret_key = os.environ.get("FI_SECRET_KEY")
    if not (api_key and secret_key):
        # fi-collector answers 401 without both, and OpenLIT only logs that.
        raise SystemExit("Set FI_API_KEY and FI_SECRET_KEY to your Future AGI keys.")
    # openlit.init() has no resource argument; its Resource.create() merges
    # OTEL_RESOURCE_ATTRIBUTES. fi-collector rejects (HTTP 400) a batch whose
    # resource has no project_name, so stop here instead.
    if not OTELResourceDetector().detect().attributes.get("project_name"):
        raise SystemExit(
            "Set OTEL_RESOURCE_ATTRIBUTES=project_name=<your project>,project_type=observe"
        )
    openlit.init(
        service_name=APP_NAME,
        # The origin, no path: the OTLP/HTTP exporter appends /v1/traces.
        otlp_endpoint=endpoint,
        # OpenLIT joins this dict into OTEL_EXPORTER_OTLP_HEADERS, which the
        # exporter splits on commas and percent-decodes: encode the values.
        otlp_headers={
            "x-api-key": quote(api_key, safe=""),
            "x-secret-key": quote(secret_key, safe=""),
        },
        capture_message_content=False,  # OpenLIT's default is True
        disable_metrics=True,  # fi-collector serves no /v1/metrics
        disable_events=True,  # events go to /v1/logs, which fi-collector does not serve
        pricing_json=str(NO_PRICING),
    )


def ask(question: str) -> str:
    # OpenAI() reads OPENAI_API_KEY and OPENAI_BASE_URL.
    response = OpenAI().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "Answer using only this policy: " + _POLICY},
            {"role": "user", "content": question},
        ],
    )
    return response.choices[0].message.content or ""


def main(argv: list[str]) -> int:
    init_tracing()
    question = argv[1] if len(argv) > 1 else "What is the refund window?"
    print(ask(question))
    # The SDK TracerProvider flushes its batch processor at exit, so a short
    # script needs no more. A long-lived server keeps exporting in batches.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
