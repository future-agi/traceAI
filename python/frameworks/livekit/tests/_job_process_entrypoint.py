"""A LiveKit job run in a real job process, for the test that checks spans survive its exit.

The job entrypoint is a module-level function because LiveKit sends it to the job process by
reference; the process imports this module as `tests._job_process_entrypoint`.
"""

import asyncio
import multiprocessing.context
import os
from typing import Any

from fi_instrumentation.otel import ProjectType, Transport, register
from livekit.agents import Agent, AgentSession, JobContext
from livekit.agents.ipc.job_proc_executor import ProcJobExecutor
from livekit.agents.job import JobAcceptArguments, RunningJobInfo
from livekit.protocol import agent, models
from opentelemetry.sdk.trace.export import SpanExportResult

from traceai_livekit import MappedHTTPSpanExporter, enable_http_attribute_mapping

UNREACHABLE_URL = "http://127.0.0.1:9"
URL_VARIABLES = (
    "FI_BASE_URL",
    "FI_GRPC_URL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
)
# Longer than any test run, so only a flush at job shutdown can export the last spans.
NEVER_MS = 60_000


def init_process(_proc: Any) -> None:
    pass


async def entrypoint(ctx: JobContext) -> None:
    """Set up tracing the way an agent does, start a session, and end the job.

    The job's metadata is the file that receives the name of every span the exporter is handed.
    The settings are made here, in the job process: a forkserver process has the environment of
    the moment the forkserver started, not of the test that asked for the job.
    """
    out_path = ctx.job.metadata
    for name in URL_VARIABLES:
        os.environ[name] = UNREACHABLE_URL
    os.environ.setdefault("FI_API_KEY", "test-api-key")
    os.environ.setdefault("FI_SECRET_KEY", "test-secret-key")

    def record_instead_of_send(self: Any, spans: Any) -> SpanExportResult:
        with open(out_path, "a") as f:
            for span in spans:
                f.write(span.name + "\n")
        return SpanExportResult.SUCCESS

    MappedHTTPSpanExporter.export = record_instead_of_send  # type: ignore[method-assign]

    register(
        project_name="livekit-job-process",
        project_type=ProjectType.OBSERVE,
        transport=Transport.HTTP,
        batch=True,
        set_global_tracer_provider=True,
        schedule_delay_millis=NEVER_MS,
        verbose=False,
    )
    enable_http_attribute_mapping()

    session = AgentSession()
    await session.start(agent=Agent(instructions="Answer briefly."))
    ctx.shutdown("call ended")


async def run_job_process(
    mp_ctx: multiprocessing.context.BaseContext, out_path: str, timeout_s: float = 60.0
) -> bool:
    """Run one job in a job process and wait for it to exit; False if it did not exit in time."""
    executor = ProcJobExecutor(
        initialize_process_fnc=init_process,
        job_entrypoint_fnc=entrypoint,
        session_end_fnc=None,
        simulation_end_fnc=None,
        inference_executor=None,
        initialize_timeout=30.0,
        close_timeout=10.0,
        session_end_timeout=10.0,
        memory_warn_mb=0,
        memory_limit_mb=0,
        ping_interval=2.5,
        ping_timeout=90.0,
        high_ping_threshold=0.5,
        http_proxy=None,
        mp_ctx=mp_ctx,
        loop=asyncio.get_running_loop(),
    )
    exited = False
    try:
        await executor.start()
        await executor.initialize()
        job = agent.Job(
            id="job-process-test",
            room=models.Room(sid="RM_test", name="test-room"),
            type=agent.JobType.JT_ROOM,
            metadata=out_path,
        )
        await executor.launch_job(
            RunningJobInfo(
                accept_arguments=JobAcceptArguments(identity="test-agent", name="", metadata=""),
                job=job,
                url="",
                token="",
                worker_id="test",
                fake_job=True,
            )
        )
        await asyncio.wait_for(executor.join(), timeout_s)
        exited = True
    except asyncio.TimeoutError:
        pass
    finally:
        # A job process that has not exited must not outlive the test.
        if not exited:
            await executor.aclose()
    return exited
