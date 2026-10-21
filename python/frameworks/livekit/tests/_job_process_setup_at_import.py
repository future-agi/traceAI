"""Tracing set up at import, the way an agent module that calls `register()` at its top does.

Only the job process imports this module, while it unpickles the job process's arguments (see
`_job_process_entrypoint.SET_UP_AT_IMPORT`); the test process never does. On Linux that is before
multiprocessing clears the exit finalizers the process started with.
"""

import os
from typing import Any, Optional

from fi_instrumentation.otel import ProjectType, Transport, register
from opentelemetry.sdk.trace.export import SpanExportResult

from traceai_livekit import MappedHTTPSpanExporter, enable_http_attribute_mapping

from ._job_process_entrypoint import NEVER_MS, UNREACHABLE_URL, URL_VARIABLES

# The file that receives the name of every span the exporter is handed; the job entrypoint sets it
# from the job's metadata. Nothing is exported before then: the batch is never sent on its own.
out_path: Optional[str] = None


def _record_instead_of_send(self: Any, spans: Any) -> SpanExportResult:
    if out_path is not None:
        with open(out_path, "a") as f:
            for span in spans:
                f.write(span.name + "\n")
    return SpanExportResult.SUCCESS


for _name in URL_VARIABLES:
    os.environ[_name] = UNREACHABLE_URL
os.environ.setdefault("FI_API_KEY", "test-api-key")
os.environ.setdefault("FI_SECRET_KEY", "test-secret-key")

MappedHTTPSpanExporter.export = _record_instead_of_send  # type: ignore[method-assign]

register(
    project_name="livekit-job-process-set-up-at-import",
    project_type=ProjectType.OBSERVE,
    transport=Transport.HTTP,
    batch=True,
    set_global_tracer_provider=True,
    schedule_delay_millis=NEVER_MS,
    verbose=False,
)
enable_http_attribute_mapping()
