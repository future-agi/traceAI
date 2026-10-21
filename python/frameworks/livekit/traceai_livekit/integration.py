"""
Integration functions for LiveKit with Future AGI.

This module provides functions to install attribute mapping for LiveKit applications
by updating existing span exporters in the tracer provider.
"""

import asyncio
import logging
import multiprocessing.util
import os
import threading
import weakref
from typing import Any, List, Optional

from fi_instrumentation.otel import Transport
from opentelemetry import trace as trace_api

from .exporters import MappedGRPCSpanExporter, MappedHTTPSpanExporter

logger = logging.getLogger(__name__)

# How long job shutdown waits for the flush, and the process-exit flush after it. OpenTelemetry's
# batch processor ignores the timeout it is given, so a collector that hangs would otherwise keep the
# job process from exiting. Together they stay well inside the 10 seconds LiveKit gives a job
# process to exit (`shutdown_process_timeout`), so LiveKit's own clean-up still runs.
_SHUTDOWN_FLUSH_TIMEOUT_S = 3.0
_EXIT_FLUSH_TIMEOUT_S = 3.0

# The providers each job already flushes at shutdown, so enabling twice registers one flush per
# provider.
_jobs_with_flush: "weakref.WeakKeyDictionary[Any, List[Any]]" = weakref.WeakKeyDictionary()

# The providers this process flushes when it exits, and the process they belong to: a forked child
# starts with its parent's list, which is not its own.
_exit_flush_providers: List[Any] = []
_exit_flush_pid: Optional[int] = None


def _flush_at_process_exit(provider: Any) -> None:
    """Flush `provider` when this process exits, after everything LiveKit does at job end.

    A LiveKit job process on Linux is a forkserver child, which ends with `os._exit()`: `atexit`
    never runs. multiprocessing still runs its own exit finalizers just before that, after the job
    has ended LiveKit's job spans and cleaned up, so the flush goes there.
    """
    global _exit_flush_pid
    if not callable(getattr(provider, "force_flush", None)):
        return
    if _exit_flush_pid != os.getpid():
        _exit_flush_pid = os.getpid()
        _exit_flush_providers.clear()
        multiprocessing.util.Finalize(None, _run_exit_flush, exitpriority=10)
    if not any(p is provider for p in _exit_flush_providers):
        _exit_flush_providers.append(provider)


def _run_exit_flush() -> None:
    """Flush every provider registered for this process, waiting at most `_EXIT_FLUSH_TIMEOUT_S`."""

    def _run() -> None:
        for provider in list(_exit_flush_providers):
            try:
                provider.force_flush()
            except Exception as e:
                logger.warning(
                    f"Flushing traces at process exit failed ({type(e).__name__}), so spans still "
                    "queued may be lost. Check that the Future AGI endpoint is reachable."
                )

    flusher = threading.Thread(target=_run, name="traceai-livekit-exit-flush", daemon=True)
    try:
        flusher.start()
    except RuntimeError:
        return
    flusher.join(_EXIT_FLUSH_TIMEOUT_S)
    if flusher.is_alive():
        logger.warning(
            f"Flushing traces at process exit took longer than {_EXIT_FLUSH_TIMEOUT_S:g} seconds, "
            "so the process exited without waiting. Check that the Future AGI endpoint is "
            "reachable; spans still queued may be lost."
        )


def flush_on_job_shutdown(provider: Optional[trace_api.TracerProvider] = None) -> bool:
    """
    Flush the tracer provider when the current LiveKit job shuts down.

    A job process on Linux ends without running `atexit`, and LiveKit flushes only its own
    processors, so spans still queued in the batch processor, the root `agent_session` span among
    them, would be lost. This sends them as soon as the job shuts down, and the provider is also
    flushed once more when the process exits, for the spans LiveKit ends after that.
    `enable_fi_attribute_mapping` calls this for you. When you set up tracing outside a job (for
    example at process start), the process-exit flush still covers you; calling this in each job
    entrypoint sends the trace earlier, before the process is at risk of being stopped.

    Do not pass `provider.force_flush` to `ctx.add_shutdown_callback`: LiveKit calls it with the
    shutdown reason, which `force_flush` takes as its timeout.

    Args:
        provider: The provider to flush. Defaults to the global tracer provider.

    Returns:
        True if the provider will be flushed when this job shuts down; False if it will not (no
        job is running, or the provider or the LiveKit version cannot do it). The process-exit
        flush is registered either way.
    """
    try:
        if provider is None:
            provider = trace_api.get_tracer_provider()
        if not callable(getattr(provider, "force_flush", None)):
            logger.debug("The tracer provider has no force_flush, so it is not flushed at job end")
            return False
        _flush_at_process_exit(provider)

        from livekit.agents import get_job_context

        # Called without arguments: `required=` only exists from livekit-agents 1.5.3, and every
        # version raises RuntimeError when there is no job.
        try:
            job_ctx = get_job_context()
        except RuntimeError:
            logger.debug(
                "No LiveKit job is running: traces are flushed when the process exits. Call "
                "traceai_livekit.flush_on_job_shutdown() inside each job entrypoint to send them "
                "when the job ends."
            )
            return False

        flushed = _jobs_with_flush.setdefault(job_ctx, [])
        if any(p is provider for p in flushed):
            return True

        # A second, non-primary AgentSession can still be closing when the shutdown callbacks
        # run, so its span may end after this flush. The primary session is closed before them.
        async def _flush_traceai(_reason: str) -> None:
            await _flush_provider(provider)

        job_ctx.add_shutdown_callback(_flush_traceai)
        flushed.append(provider)
        return True
    except Exception as e:
        logger.debug(f"Could not register the flush for job shutdown: {type(e).__name__}")
        return False


async def _flush_provider(provider: Any) -> None:
    """Flush `provider` off the event loop, waiting at most `_SHUTDOWN_FLUSH_TIMEOUT_S`.

    A plain thread, not `asyncio.to_thread`: LiveKit joins the loop's default executor before the
    job process exits, so a stuck flush there would hold the process open. A daemon thread is
    simply left behind when the wait runs out.
    """
    loop = asyncio.get_running_loop()
    done: "asyncio.Future[Optional[Exception]]" = loop.create_future()

    def _resolve(error: Optional[Exception]) -> None:
        if not done.done():
            done.set_result(error)

    def _run() -> None:
        error: Optional[Exception] = None
        try:
            provider.force_flush()
        except Exception as e:
            error = e
        try:
            loop.call_soon_threadsafe(_resolve, error)
        except RuntimeError:
            # The flush outlived the wait and the loop is already closed; nobody is waiting.
            pass

    try:
        threading.Thread(target=_run, name="traceai-livekit-flush", daemon=True).start()
    except RuntimeError as e:
        logger.warning(
            f"Could not start the thread that flushes traces at job shutdown ({type(e).__name__}), "
            "so spans still queued may be lost."
        )
        return

    try:
        error = await asyncio.wait_for(done, _SHUTDOWN_FLUSH_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning(
            f"Flushing traces at job shutdown took longer than {_SHUTDOWN_FLUSH_TIMEOUT_S:g} "
            "seconds, so the job ended without waiting. Check that the Future AGI endpoint "
            "is reachable; spans still queued may be lost."
        )
        return
    if error is not None:
        logger.warning(
            f"Flushing traces at job shutdown failed ({type(error).__name__}), so spans still "
            "queued may be lost. Check that the Future AGI endpoint is reachable."
        )


def enable_fi_attribute_mapping(transport: Transport = Transport.HTTP) -> bool:
    """
    Install the mapping exporter by swapping FI's exporter instances.

    This function:
    1. Finds existing span exporters in the global tracer provider
    2. Replaces them with mapped exporters that convert LiveKit attributes to Future AGI conventions.
    3. Automatically registers the global tracer provider with `livekit.agents.telemetry`.
    4. Flushes the provider when the LiveKit job shuts down, if it runs inside one, and when the
       process exits (see `flush_on_job_shutdown`).

    Args:
        transport: The transport protocol to match existing exporters (Transport enum)

    Returns:
        True if at least one exporter was swapped; False otherwise.
    """
    provider = trace_api.get_tracer_provider()
    swapped_any = False

    active = getattr(provider, "_active_span_processor", None)
    if not active:
        logger.warning("No active span processor found")
        return False

    # Handle both single processor and MultiSpanProcessor cases
    processors = []
    if hasattr(active, "_span_processors"):
        processors = list(active._span_processors)
    else:
        processors = [active]

    for proc in processors:
        # Look for exporter in standard places
        exporter = getattr(proc, "span_exporter", None)
        if exporter is None:
            exporter = getattr(proc, "_exporter", None)
            
        if exporter is None:
            continue

        try:
            if transport == Transport.HTTP:
                from fi_instrumentation.otel import (
                    HTTPSpanExporter as _FIHTTPSpanExporter,
                )

                if isinstance(exporter, _FIHTTPSpanExporter):
                    endpoint = getattr(exporter, "_endpoint", None)
                    headers = getattr(exporter, "_headers", None)
                    new_exporter = MappedHTTPSpanExporter(
                        endpoint=endpoint, headers=headers
                    )

                    # Replace exporter in the processor
                    if hasattr(proc, "_batch_processor"):
                         # Some batch processor implementations wrap logic
                         setattr(proc._batch_processor, "_exporter", new_exporter)
                    elif hasattr(proc, "span_exporter"):
                        try:
                             setattr(proc, "span_exporter", new_exporter)
                        except AttributeError:
                             # Fallback for read-only property, try internal
                             if hasattr(proc, "_exporter"):
                                  setattr(proc, "_exporter", new_exporter)
                    elif hasattr(proc, "_exporter"):
                        setattr(proc, "_exporter", new_exporter)

                    swapped_any = True
                    logger.info("Replaced HTTP exporter with mapped LiveKit HTTP exporter")

            elif transport == Transport.GRPC:
                from fi_instrumentation.otel import (
                    GRPCSpanExporter as _FIGRPCSpanExporter,
                )

                if isinstance(exporter, _FIGRPCSpanExporter):
                    endpoint = getattr(exporter, "_endpoint", None)
                    headers = getattr(exporter, "_headers", None)
                    new_exporter = MappedGRPCSpanExporter(
                        endpoint=endpoint, headers=headers
                    )

                    if hasattr(proc, "_batch_processor"):
                         setattr(proc._batch_processor, "_exporter", new_exporter)
                    elif hasattr(proc, "span_exporter"):
                        try:
                             setattr(proc, "span_exporter", new_exporter)
                        except AttributeError:
                             if hasattr(proc, "_exporter"):
                                  setattr(proc, "_exporter", new_exporter)
                    elif hasattr(proc, "_exporter"):
                        setattr(proc, "_exporter", new_exporter)

                    swapped_any = True
                    logger.info("Replaced gRPC exporter with mapped LiveKit gRPC exporter")

        except ImportError as e:
            logger.warning(f"Failed to import required exporter: {e}")
            continue
        except Exception as e:
            logger.warning(f"Failed to replace exporter: {e}")
            continue

    if not swapped_any:
        logger.warning(f"No {transport.value} exporters found to replace")
    
    # `register()` marks its own exporter as a default that the next `add_span_processor` call
    # replaces. LiveKit adds its privacy filter through `add_span_processor`, which would shut the
    # exporter down and send nothing. Keep it: processors added from here on run alongside it.
    if getattr(provider, "_default_processor", False):
        provider._default_processor = False

    # Automatically set the tracer provider for LiveKit agents
    try:
        from livekit.agents.telemetry import set_tracer_provider
        set_tracer_provider(provider)
        logger.info("Automatically configured LiveKit agents tracer provider")
    except ImportError:
        logger.warning("livekit.agents.telemetry not found. Skipping set_tracer_provider.")
    except Exception as e:
        logger.warning(f"Failed to set LiveKit tracer provider: {e}")

    flush_on_job_shutdown(provider)

    return swapped_any


def enable_http_attribute_mapping() -> bool:
    """
    Install HTTP attribute mapping for LiveKit and configure the LiveKit tracer provider.

    Convenience function that calls enable_fi_attribute_mapping with HTTP transport.

    Returns:
        True if at least one exporter was swapped; False otherwise.
    """
    return enable_fi_attribute_mapping(transport=Transport.HTTP)


def enable_grpc_attribute_mapping() -> bool:
    """
    Install gRPC attribute mapping for LiveKit and configure the LiveKit tracer provider.

    Convenience function that calls enable_fi_attribute_mapping with gRPC transport.

    Returns:
        True if at least one exporter was swapped; False otherwise.
    """
    return enable_fi_attribute_mapping(transport=Transport.GRPC)


def create_mapped_http_exporter(
    endpoint: Optional[str] = None, headers: Optional[dict] = None
):
    """
    Create a mapped HTTP exporter for LiveKit.

    This function creates a new HTTP exporter that maps LiveKit attributes
    to Future AGI conventions. It can be used independently without
    modifying existing exporters.

    Args:
        endpoint: The collector endpoint (optional, will use environment defaults)
        headers: Optional headers to include in requests (optional, will use environment defaults)

    Returns:
        A configured MappedHTTPSpanExporter instance
    """
    return MappedHTTPSpanExporter(endpoint=endpoint, headers=headers)


def create_mapped_grpc_exporter(
    endpoint: Optional[str] = None, headers: Optional[dict] = None
):
    """
    Create a mapped gRPC exporter for LiveKit.

    This function creates a new gRPC exporter that maps LiveKit attributes
    to Future AGI conventions. It can be used independently without
    modifying existing exporters.

    Args:
        endpoint: The collector endpoint (optional, will use environment defaults)
        headers: Optional headers to include in requests (optional, will use environment defaults)

    Returns:
        A configured MappedGRPCSpanExporter instance
    """
    return MappedGRPCSpanExporter(endpoint=endpoint, headers=headers)
