"""A test fixture, not part of the recipe: the recipe's traced functions
exported through LangSmith's own tracer provider instead of ``init_tracing()``.

The contract test runs it to show what that provider puts on the wire: a
resource without ``project_name`` even when ``OTEL_RESOURCE_ATTRIBUTES`` sets
one, and, without ``OTEL_EXPORTER_OTLP_HEADERS``, LangSmith's own
``x-api-key`` and ``Langsmith-Project`` headers.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import langsmith  # noqa: E402
from app import support_request  # noqa: E402
from opentelemetry import trace  # noqa: E402

# No global provider exists yet, so the client installs LangSmith's own.
client = langsmith.Client()
langsmith.configure(client=client)
print(support_request(sys.argv[1]))
client.flush()
trace.get_tracer_provider().shutdown()
