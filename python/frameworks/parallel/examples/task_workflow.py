"""Application spans cover Task submission AND waiting, not server internals.

Set PARALLEL_API_KEY, FI_API_KEY and FI_SECRET_KEY before running.
Use --async for the AsyncParallel example. Task content and raw error text
are deliberately not attached to these metadata-only workflow spans.
"""

import asyncio
import sys

from fi_instrumentation import FITracer, register
from fi_instrumentation.fi_types import ProjectType
from opentelemetry.trace import Status, StatusCode
from parallel import AsyncParallel, Parallel
from parallel.types import TaskRunResult


def research(client: Parallel, tracer: FITracer, question: str) -> TaskRunResult:
    with tracer.start_as_current_span(
        "parallel.task", attributes={"gen_ai.span.kind": "CHAIN", "gen_ai.provider.name": "parallel"},
        record_exception=False, set_status_on_exception=False,
    ) as span:
        try:
            run = client.task_run.create(input=question, processor="base")
            span.set_attribute("parallel.run_id", run.run_id)
            span.set_attribute("parallel.processor", "base")
            result = client.task_run.result(run.run_id, api_timeout=600, timeout=610)
        except Exception:
            span.set_status(Status(StatusCode.ERROR, "Task request failed"))
            raise
        span.set_attribute("parallel.task.status", result.run.status)
        span.set_status(Status(StatusCode.OK) if result.run.status == "completed"
                        else Status(StatusCode.ERROR, "Task did not complete"))
        return result


async def research_async(client: AsyncParallel, tracer: FITracer, question: str) -> TaskRunResult:
    with tracer.start_as_current_span(
        "parallel.task", attributes={"gen_ai.span.kind": "CHAIN", "gen_ai.provider.name": "parallel"},
        record_exception=False, set_status_on_exception=False,
    ) as span:
        try:
            run = await client.task_run.create(input=question, processor="base")
            span.set_attribute("parallel.run_id", run.run_id)
            span.set_attribute("parallel.processor", "base")
            result = await client.task_run.result(run.run_id, api_timeout=600, timeout=610)
        except asyncio.CancelledError:
            span.set_attribute("parallel.cancelled", True)
            span.set_status(Status(StatusCode.ERROR, "Task wait cancelled"))
            raise
        except Exception:
            span.set_status(Status(StatusCode.ERROR, "Task request failed"))
            raise
        span.set_attribute("parallel.task.status", result.run.status)
        span.set_status(Status(StatusCode.OK) if result.run.status == "completed"
                        else Status(StatusCode.ERROR, "Task did not complete"))
        return result


def main() -> None:
    provider = register(project_name="parallel-tasks", project_type=ProjectType.OBSERVE)
    tracer = FITracer(provider.get_tracer("parallel-workflow"))
    question = "What is OpenTelemetry and who maintains it?"

    async def run_async() -> None:
        async with AsyncParallel() as client:
            result = await research_async(client, tracer, question)
            print(result.output.content)

    try:
        if "--async" in sys.argv:
            asyncio.run(run_async())
        else:
            with Parallel() as client:
                result = research(client, tracer, question)
                print(result.output.content)
    finally:
        provider.force_flush()
        provider.shutdown()


if __name__ == "__main__":
    main()
