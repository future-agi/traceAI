"""Checkpoint operation tracing for LangGraph.

Traces checkpoint save/load operations for persistence and recovery.
"""

import functools
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from opentelemetry import trace as trace_api
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from traceai_langchain._langgraph._attributes import LangGraphAttributes, LangGraphSpanKind
from traceai_langchain._langgraph._state_tracker import safe_json_dumps, get_object_size


class CheckpointOperation:
    """Information about a checkpoint operation."""

    def __init__(
        self,
        operation: str,  # "save", "load", "list"
        thread_id: Optional[str] = None,
        checkpoint_id: Optional[str] = None,
    ):
        self.operation = operation
        self.thread_id = thread_id
        self.checkpoint_id = checkpoint_id
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.duration_ms: Optional[float] = None
        self.size_bytes: Optional[int] = None
        self.success: bool = False
        self.error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "operation": self.operation,
            "thread_id": self.thread_id,
            "checkpoint_id": self.checkpoint_id,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
            "size_bytes": self.size_bytes,
            "success": self.success,
            "error": self.error,
        }


class CheckpointTracer:
    """Trace checkpoint operations in LangGraph.

    Wraps BaseCheckpointSaver methods to capture:
    - Checkpoint save operations
    - Checkpoint load operations
    - Checkpoint listing operations
    - Size and duration metrics
    """

    def __init__(self, tracer: trace_api.Tracer):
        """Initialize the checkpoint tracer.

        Args:
            tracer: OpenTelemetry tracer
        """
        self._tracer = tracer
        self._operation_history: List[CheckpointOperation] = []
        self._original_methods: Dict[str, Callable] = {}

    def wrap_checkpoint_saver(self, checkpoint_saver: Any) -> Any:
        """Wrap a checkpoint saver with tracing.

        Args:
            checkpoint_saver: The checkpoint saver instance

        Returns:
            The wrapped checkpoint saver
        """
        backend_name = type(checkpoint_saver).__name__

        # Wrap put method (save)
        if hasattr(checkpoint_saver, 'put'):
            original_put = checkpoint_saver.put
            self._original_methods['put'] = original_put
            checkpoint_saver.put = self._create_put_wrapper(original_put, backend_name)

        # Wrap get method (load)
        if hasattr(checkpoint_saver, 'get'):
            original_get = checkpoint_saver.get
            self._original_methods['get'] = original_get
            checkpoint_saver.get = self._create_get_wrapper(original_get, backend_name)

        # Wrap list method
        if hasattr(checkpoint_saver, 'list'):
            original_list = checkpoint_saver.list
            self._original_methods['list'] = original_list
            checkpoint_saver.list = self._create_list_wrapper(original_list, backend_name)

        # Wrap async methods if available
        if hasattr(checkpoint_saver, 'aput'):
            original_aput = checkpoint_saver.aput
            self._original_methods['aput'] = original_aput
            checkpoint_saver.aput = self._create_async_put_wrapper(original_aput, backend_name)

        if hasattr(checkpoint_saver, 'aget'):
            original_aget = checkpoint_saver.aget
            self._original_methods['aget'] = original_aget
            checkpoint_saver.aget = self._create_async_get_wrapper(original_aget, backend_name)

        return checkpoint_saver

    def _create_put_wrapper(
        self,
        original_put: Callable,
        backend_name: str,
    ) -> Callable:
        """Create wrapper for checkpoint put (save) method.

        Args:
            original_put: Original put method
            backend_name: Name of the checkpoint backend

        Returns:
            Wrapped put method
        """
        tracer = self._tracer
        operation_history = self._operation_history

        @functools.wraps(original_put)
        def wrapped(config: Dict[str, Any], checkpoint: Any, metadata: Optional[Dict] = None):
            thread_id = config.get("configurable", {}).get("thread_id")
            operation = CheckpointOperation("save", thread_id=str(thread_id) if thread_id else None)

            with tracer.start_as_current_span(
                "langgraph.checkpoint.save",
                kind=SpanKind.INTERNAL,
            ) as span:
                start_time = time.perf_counter()

                try:
                    span.set_attribute("gen_ai.span.kind", LangGraphSpanKind.CHECKPOINT)
                    span.set_attribute(LangGraphAttributes.CHECKPOINT_OPERATION, "save")
                    span.set_attribute(LangGraphAttributes.CHECKPOINT_BACKEND, backend_name)

                    if thread_id:
                        span.set_attribute(
                            LangGraphAttributes.CHECKPOINT_THREAD_ID,
                            str(thread_id)
                        )

                    # Calculate checkpoint size
                    checkpoint_size = get_object_size(checkpoint)
                    span.set_attribute(
                        LangGraphAttributes.CHECKPOINT_SIZE_BYTES,
                        checkpoint_size
                    )
                    operation.size_bytes = checkpoint_size

                    # Execute save
                    result = original_put(config, checkpoint, metadata)

                    # Record checkpoint ID if returned
                    if result:
                        span.set_attribute(
                            LangGraphAttributes.CHECKPOINT_ID,
                            str(result)
                        )
                        operation.checkpoint_id = str(result)

                    end_time = time.perf_counter()
                    duration_ms = (end_time - start_time) * 1000

                    span.set_attribute(LangGraphAttributes.PERF_DURATION_MS, duration_ms)
                    operation.duration_ms = duration_ms
                    operation.success = True

                    span.add_event("checkpoint_saved", {
                        "thread_id": str(thread_id) if thread_id else None,
                        "checkpoint_id": str(result) if result else None,
                        "size_bytes": checkpoint_size,
                        "backend": backend_name,
                    })

                    span.set_status(Status(StatusCode.OK))
                    return result

                except Exception as e:
                    end_time = time.perf_counter()
                    duration_ms = (end_time - start_time) * 1000

                    operation.duration_ms = duration_ms
                    operation.error = str(e)

                    span.set_attribute(LangGraphAttributes.PERF_DURATION_MS, duration_ms)
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    span.record_exception(e)
                    raise

                finally:
                    operation_history.append(operation)

        return wrapped

    def _create_get_wrapper(
        self,
        original_get: Callable,
        backend_name: str,
    ) -> Callable:
        """Create wrapper for checkpoint get (load) method.

        Args:
            original_get: Original get method
            backend_name: Name of the checkpoint backend

        Returns:
            Wrapped get method
        """
        tracer = self._tracer
        operation_history = self._operation_history

        @functools.wraps(original_get)
        def wrapped(config: Dict[str, Any]):
            thread_id = config.get("configurable", {}).get("thread_id")
            checkpoint_id = config.get("configurable", {}).get("checkpoint_id")
            operation = CheckpointOperation(
                "load",
                thread_id=str(thread_id) if thread_id else None,
                checkpoint_id=str(checkpoint_id) if checkpoint_id else None,
            )

            with tracer.start_as_current_span(
                "langgraph.checkpoint.load",
                kind=SpanKind.INTERNAL,
            ) as span:
                start_time = time.perf_counter()

                try:
                    span.set_attribute("gen_ai.span.kind", LangGraphSpanKind.CHECKPOINT)
                    span.set_attribute(LangGraphAttributes.CHECKPOINT_OPERATION, "load")
                    span.set_attribute(LangGraphAttributes.CHECKPOINT_BACKEND, backend_name)

                    if thread_id:
                        span.set_attribute(
                            LangGraphAttributes.CHECKPOINT_THREAD_ID,
                            str(thread_id)
                        )

                    if checkpoint_id:
                        span.set_attribute(
                            LangGraphAttributes.CHECKPOINT_ID,
                            str(checkpoint_id)
                        )

                    # Execute load
                    result = original_get(config)

                    end_time = time.perf_counter()
                    duration_ms = (end_time - start_time) * 1000

                    span.set_attribute(LangGraphAttributes.PERF_DURATION_MS, duration_ms)
                    operation.duration_ms = duration_ms

                    if result is not None:
                        span.set_attribute(LangGraphAttributes.CHECKPOINT_FOUND, True)
                        checkpoint_size = get_object_size(result)
                        span.set_attribute(
                            LangGraphAttributes.CHECKPOINT_SIZE_BYTES,
                            checkpoint_size
                        )
                        operation.size_bytes = checkpoint_size
                        operation.success = True
                    else:
                        span.set_attribute(LangGraphAttributes.CHECKPOINT_FOUND, False)
                        operation.success = True  # No error, just no checkpoint

                    span.add_event("checkpoint_loaded", {
                        "thread_id": str(thread_id) if thread_id else None,
                        "found": result is not None,
                        "backend": backend_name,
                    })

                    span.set_status(Status(StatusCode.OK))
                    return result

                except Exception as e:
                    end_time = time.perf_counter()
                    duration_ms = (end_time - start_time) * 1000

                    operation.duration_ms = duration_ms
                    operation.error = str(e)

                    span.set_attribute(LangGraphAttributes.PERF_DURATION_MS, duration_ms)
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    span.record_exception(e)
                    raise

                finally:
                    operation_history.append(operation)

        return wrapped

    def _create_list_wrapper(
        self,
        original_list: Callable,
        backend_name: str,
    ) -> Callable:
        """Create wrapper for checkpoint list method.

        Args:
            original_list: Original list method
            backend_name: Name of the checkpoint backend

        Returns:
            Wrapped list method
        """
        tracer = self._tracer
        operation_history = self._operation_history

        @functools.wraps(original_list)
        def wrapped(config: Dict[str, Any], **kwargs):
            thread_id = config.get("configurable", {}).get("thread_id")
            operation = CheckpointOperation("list", thread_id=str(thread_id) if thread_id else None)

            with tracer.start_as_current_span(
                "langgraph.checkpoint.list",
                kind=SpanKind.INTERNAL,
            ) as span:
                start_time = time.perf_counter()

                try:
                    span.set_attribute("gen_ai.span.kind", LangGraphSpanKind.CHECKPOINT)
                    span.set_attribute(LangGraphAttributes.CHECKPOINT_OPERATION, "list")
                    span.set_attribute(LangGraphAttributes.CHECKPOINT_BACKEND, backend_name)

                    if thread_id:
                        span.set_attribute(
                            LangGraphAttributes.CHECKPOINT_THREAD_ID,
                            str(thread_id)
                        )

                    # Execute list
                    results = list(original_list(config, **kwargs))

                    end_time = time.perf_counter()
                    duration_ms = (end_time - start_time) * 1000

                    span.set_attribute(LangGraphAttributes.PERF_DURATION_MS, duration_ms)
                    span.set_attribute("langgraph.checkpoint.list_count", len(results))
                    operation.duration_ms = duration_ms
                    operation.success = True

                    span.add_event("checkpoints_listed", {
                        "thread_id": str(thread_id) if thread_id else None,
                        "count": len(results),
                        "backend": backend_name,
                    })

                    span.set_status(Status(StatusCode.OK))

                    # Yield results to maintain generator behavior
                    for r in results:
                        yield r

                except Exception as e:
                    end_time = time.perf_counter()
                    duration_ms = (end_time - start_time) * 1000

                    operation.duration_ms = duration_ms
                    operation.error = str(e)

                    span.set_attribute(LangGraphAttributes.PERF_DURATION_MS, duration_ms)
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    span.record_exception(e)
                    raise

                finally:
                    operation_history.append(operation)

        return wrapped

    def _create_async_put_wrapper(
        self,
        original_aput: Callable,
        backend_name: str,
    ) -> Callable:
        """Create async wrapper for checkpoint put method."""
        tracer = self._tracer
        operation_history = self._operation_history

        @functools.wraps(original_aput)
        async def wrapped(config: Dict[str, Any], checkpoint: Any, metadata: Optional[Dict] = None):
            thread_id = config.get("configurable", {}).get("thread_id")
            operation = CheckpointOperation("save", thread_id=str(thread_id) if thread_id else None)

            with tracer.start_as_current_span(
                "langgraph.checkpoint.save",
                kind=SpanKind.INTERNAL,
            ) as span:
                start_time = time.perf_counter()

                try:
                    span.set_attribute("gen_ai.span.kind", LangGraphSpanKind.CHECKPOINT)
                    span.set_attribute(LangGraphAttributes.CHECKPOINT_OPERATION, "save")
                    span.set_attribute(LangGraphAttributes.CHECKPOINT_BACKEND, backend_name)

                    if thread_id:
                        span.set_attribute(
                            LangGraphAttributes.CHECKPOINT_THREAD_ID,
                            str(thread_id)
                        )

                    checkpoint_size = get_object_size(checkpoint)
                    span.set_attribute(
                        LangGraphAttributes.CHECKPOINT_SIZE_BYTES,
                        checkpoint_size
                    )
                    operation.size_bytes = checkpoint_size

                    result = await original_aput(config, checkpoint, metadata)

                    if result:
                        span.set_attribute(
                            LangGraphAttributes.CHECKPOINT_ID,
                            str(result)
                        )
                        operation.checkpoint_id = str(result)

                    end_time = time.perf_counter()
                    duration_ms = (end_time - start_time) * 1000

                    span.set_attribute(LangGraphAttributes.PERF_DURATION_MS, duration_ms)
                    operation.duration_ms = duration_ms
                    operation.success = True

                    span.set_status(Status(StatusCode.OK))
                    return result

                except Exception as e:
                    end_time = time.perf_counter()
                    operation.duration_ms = (end_time - start_time) * 1000
                    operation.error = str(e)

                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    span.record_exception(e)
                    raise

                finally:
                    operation_history.append(operation)

        return wrapped

    def _create_async_get_wrapper(
        self,
        original_aget: Callable,
        backend_name: str,
    ) -> Callable:
        """Create async wrapper for checkpoint get method."""
        tracer = self._tracer
        operation_history = self._operation_history

        @functools.wraps(original_aget)
        async def wrapped(config: Dict[str, Any]):
            thread_id = config.get("configurable", {}).get("thread_id")
            operation = CheckpointOperation("load", thread_id=str(thread_id) if thread_id else None)

            with tracer.start_as_current_span(
                "langgraph.checkpoint.load",
                kind=SpanKind.INTERNAL,
            ) as span:
                start_time = time.perf_counter()

                try:
                    span.set_attribute("gen_ai.span.kind", LangGraphSpanKind.CHECKPOINT)
                    span.set_attribute(LangGraphAttributes.CHECKPOINT_OPERATION, "load")
                    span.set_attribute(LangGraphAttributes.CHECKPOINT_BACKEND, backend_name)

                    if thread_id:
                        span.set_attribute(
                            LangGraphAttributes.CHECKPOINT_THREAD_ID,
                            str(thread_id)
                        )

                    result = await original_aget(config)

                    end_time = time.perf_counter()
                    duration_ms = (end_time - start_time) * 1000

                    span.set_attribute(LangGraphAttributes.PERF_DURATION_MS, duration_ms)
                    operation.duration_ms = duration_ms

                    if result is not None:
                        span.set_attribute(LangGraphAttributes.CHECKPOINT_FOUND, True)
                        operation.success = True
                    else:
                        span.set_attribute(LangGraphAttributes.CHECKPOINT_FOUND, False)
                        operation.success = True

                    span.set_status(Status(StatusCode.OK))
                    return result

                except Exception as e:
                    end_time = time.perf_counter()
                    operation.duration_ms = (end_time - start_time) * 1000
                    operation.error = str(e)

                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    span.record_exception(e)
                    raise

                finally:
                    operation_history.append(operation)

        return wrapped

    def get_operation_history(self) -> List[Dict[str, Any]]:
        """Get the checkpoint operation history.

        Returns:
            List of operation dictionaries
        """
        return [op.to_dict() for op in self._operation_history]

    def get_stats(self) -> Dict[str, Any]:
        """Get checkpoint operation statistics.

        Returns:
            Dictionary with operation statistics
        """
        if not self._operation_history:
            return {"error": "No checkpoint operations recorded"}

        saves = [op for op in self._operation_history if op.operation == "save"]
        loads = [op for op in self._operation_history if op.operation == "load"]

        save_durations = [op.duration_ms for op in saves if op.duration_ms]
        load_durations = [op.duration_ms for op in loads if op.duration_ms]
        save_sizes = [op.size_bytes for op in saves if op.size_bytes]

        return {
            "total_operations": len(self._operation_history),
            "save_count": len(saves),
            "load_count": len(loads),
            "successful_saves": sum(1 for op in saves if op.success),
            "successful_loads": sum(1 for op in loads if op.success),
            "avg_save_duration_ms": sum(save_durations) / len(save_durations) if save_durations else 0,
            "avg_load_duration_ms": sum(load_durations) / len(load_durations) if load_durations else 0,
            "avg_checkpoint_size_bytes": sum(save_sizes) / len(save_sizes) if save_sizes else 0,
            "total_checkpoint_size_bytes": sum(save_sizes) if save_sizes else 0,
        }

    def reset(self) -> None:
        """Reset the operation history."""
        self._operation_history.clear()
