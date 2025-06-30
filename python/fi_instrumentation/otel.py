import atexit
import inspect
import json
import math
import os
import signal
import sys
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Type, Union
from urllib.parse import ParseResult, urlparse

import requests
from fi_instrumentation.fi_types import (
    EvalName,
    EvalTag,
    ProjectType,
    prepare_eval_tags,
)
from fi_instrumentation.instrumentation.constants import (
    DEFAULT_MAX_ACTIVE_SPANS_TRACKED,
    FI_MAX_ACTIVE_SPANS_TRACKED,
)
from fi_instrumentation.settings import (
    UuidIdGenerator,
    get_env_collector_endpoint,
    get_env_fi_auth_header,
    get_env_grpc_collector_endpoint,
    get_env_project_name,
    get_env_project_version_name,
)
from jsonschema import ValidationError
from opentelemetry import trace as trace_api
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter as _GRPCSpanExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter as _HTTPSpanExporter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.sdk.trace import TracerProvider as _TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor as _BatchSpanProcessor
from opentelemetry.sdk.trace.export import SimpleSpanProcessor as _SimpleSpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

PROJECT_NAME = "project_name"
PROJECT_TYPE = "project_type"
PROJECT_VERSION_NAME = "project_version_name"
PROJECT_VERSION_ID = "project_version_id"
EVAL_TAGS = "eval_tags"
METADATA = "metadata"
SESSION_NAME = "session_name"

CONTENT_TYPE = "Content-Type"
AUTHORIZATION = "authorization"


class Transport(str, Enum):
    GRPC = "grpc"
    HTTP = "http"


def register(
    *,
    project_name: Optional[str] = None,
    project_type: Optional[ProjectType] = ProjectType.EXPERIMENT,
    project_version_name: Optional[str] = None,
    eval_tags: Optional[List[EvalTag]] = None,
    session_name: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    batch: bool = False,
    set_global_tracer_provider: bool = False,
    headers: Optional[Dict[str, str]] = None,
    verbose: bool = True,
    transport: Transport = Transport.HTTP,
) -> _TracerProvider:

    eval_tags = eval_tags or []
    metadata = metadata or {}

    if eval_tags:
        for tag in eval_tags:
            if isinstance(tag.eval_name, EvalName):
                tag.eval_name = tag.eval_name.value
        eval_tags = prepare_eval_tags(eval_tags)

    if project_type == ProjectType.OBSERVE:
        if eval_tags:
            raise ValidationError("Eval tags are not allowed for project type OBSERVE")
        if project_version_name:
            raise ValidationError(
                "Project Version Name not allowed for project type OBSERVE"
            )

    if project_type == ProjectType.EXPERIMENT:
        if session_name:
            raise ValidationError(
                "Session name is not allowed for project type EXPERIMENT"
            )

    project_name = project_name or get_env_project_name()
    project_version_name = project_version_name or get_env_project_version_name()
    project_version_id = str(uuid.uuid4())

    custom_eval_names = [tag["custom_eval_name"] for tag in eval_tags]

    # Check for duplicate custom eval names
    if len(custom_eval_names) != len(set(custom_eval_names)):
        raise ValidationError("Duplicate custom eval names are not allowed")

    custom_eval_exists = check_custom_eval_config_exists(
        project_name=project_name,
        eval_tags=eval_tags,
    )

    if custom_eval_exists:
        raise ValidationError(
            "Custom eval configuration already exists for this project"
        )

    resource_attributes = {
        PROJECT_NAME: project_name,
        PROJECT_TYPE: project_type.value,
        PROJECT_VERSION_NAME: project_version_name,
        PROJECT_VERSION_ID: project_version_id,
        EVAL_TAGS: json.dumps(eval_tags),
        METADATA: json.dumps(metadata),
    }

    if project_type == ProjectType.OBSERVE and session_name is not None:
        resource_attributes[SESSION_NAME] = session_name

    resource = Resource(attributes=resource_attributes)

    tracer_provider = TracerProvider(
        resource=resource, verbose=False, id_generator=UuidIdGenerator(),
        transport=transport
    )
    span_processor: SpanProcessor
    if batch:
        span_processor = BatchSpanProcessor(
            headers=headers,
            transport=transport,
        )
    else:
        span_processor = SimpleSpanProcessor(
            headers=headers,
            transport=transport,
        )
    tracer_provider.add_span_processor(span_processor)
    tracer_provider._default_processor = True

    if set_global_tracer_provider:
        trace_api.set_tracer_provider(tracer_provider)
        global_provider_msg = (
            "|  \n"
            "|  `register` has set this TracerProvider as the global OpenTelemetry default.\n"
            "|  To disable this behavior, call `register` with "
            "`set_global_tracer_provider=False`.\n"
        )
    else:
        global_provider_msg = ""

    tracer_provider.setup_signal_handlers()

    details = tracer_provider._tracing_details()
    if verbose:
        print(f"{details}" f"{global_provider_msg}")
    return tracer_provider


class TracerProvider(_TracerProvider):
    """
    An extension of `opentelemetry.sdk.trace.TracerProvider` with Future AGI aware defaults.

    Extended keyword arguments are documented in the `Args` section. For further documentation, see
    the OpenTelemetry documentation at https://opentelemetry.io/docs/specs/otel/trace/sdk/.

    Args:
        endpoint (str, optional): The collector endpoint to which spans will be exported. If
            specified, a default SpanProcessor will be created and added to this TracerProvider.
            If not provided, the `BASE_URL` environment variable will be
            used to infer which collector endpoint to use, defaults to the gRPC endpoint. When
            specifying the endpoint, the transport method (HTTP or gRPC) will be inferred from the
            URL.
        verbose (bool): If True, configuration details will be printed to stdout.
    """

    def __init__(
        self,
        *args: Any,
        verbose: bool = True,
        transport: Transport = Transport.HTTP,
        **kwargs: Any,
    ):
        if "shutdown_on_exit" not in kwargs:
            kwargs["shutdown_on_exit"] = True

        sig = _get_class_signature(_TracerProvider)
        bound_args = sig.bind_partial(*args, **kwargs)
        bound_args.apply_defaults()
        if bound_args.arguments.get("resource") is None:
            bound_args.arguments["resource"] = Resource.create(
                {
                    PROJECT_NAME: get_env_project_name(),
                    PROJECT_TYPE: ProjectType.EXPERIMENT.value,
                    PROJECT_VERSION_NAME: get_env_project_version_name(),
                }
            )
        super().__init__(*bound_args.args, **bound_args.kwargs)

        self._default_processor = False


        if transport == Transport.HTTP:
            endpoint = get_env_collector_endpoint()
            parsed_url, endpoint = _normalized_endpoint(endpoint)
            exporter: SpanExporter = HTTPSpanExporter(endpoint=endpoint)
        elif transport == Transport.GRPC:
            endpoint = get_env_grpc_collector_endpoint()
            exporter: SpanExporter = GRPCSpanExporter(endpoint=endpoint)
        else:
            raise ValueError(f"Invalid transport: {transport}")

        self.add_span_processor(SimpleSpanProcessor(span_exporter=exporter))
        self._default_processor = True
        if verbose:
            print(self._tracing_details())

    def add_span_processor(self, *args: Any, **kwargs: Any) -> None:
        """
        Registers a new `SpanProcessor` for this `TracerProvider`.

        If this `TracerProvider` has a default processor, it will be removed.
        """

        if self._default_processor:
            self._active_span_processor.shutdown()
            self._active_span_processor._span_processors = tuple()
            self._default_processor = False
        return super().add_span_processor(*args, **kwargs)

    def _tracing_details(self) -> str:
        project = self.resource.attributes.get(PROJECT_NAME)
        project_type = self.resource.attributes.get(PROJECT_TYPE)
        project_version_name = self.resource.attributes.get(PROJECT_VERSION_NAME)
        eval_tags = self.resource.attributes.get(EVAL_TAGS)
        session_name = self.resource.attributes.get(SESSION_NAME)

        processor_name: Optional[str] = None
        endpoint: Optional[str] = None
        transport: Optional[str] = None
        headers: Optional[Union[Dict[str, str], str]] = None

        if self._active_span_processor:
            if processors := self._active_span_processor._span_processors:
                if len(processors) == 1:
                    span_processor = self._active_span_processor._span_processors[0]
                    if exporter := getattr(span_processor, "span_exporter"):
                        processor_name = span_processor.__class__.__name__
                        endpoint = exporter._endpoint
                        transport = _exporter_transport(exporter)
                        headers = _printable_headers(exporter._headers)
                else:
                    processor_name = "Multiple Span Processors"
                    endpoint = "Multiple Span Exporters"
                    transport = "Multiple Span Exporters"
                    headers = "Multiple Span Exporters"

        if os.name == "nt":
            details_header = "OpenTelemetry Tracing Details"
        else:
            details_header = "🔭 OpenTelemetry Tracing Details 🔭"

        configuration_msg = "|  Using a default SpanProcessor. `add_span_processor` will overwrite this default.\n"

        details_msg = (
            f"{details_header}\n"
            f"|  FI Project: {project}\n"
            f"|  FI Project Type: {project_type}\n"
            f"|  FI Project Version Name: {project_version_name}\n"
            f"|  Span Processor: {processor_name}\n"
            f"|  Collector Endpoint: {endpoint}\n"
            f"|  Transport: {transport}\n"
            f"|  Transport Headers: {headers}\n"
            f"|  Eval Tags: {eval_tags}\n"
            f"|  Session Name: {session_name}\n"
            "|  \n"
            f"{configuration_msg if self._default_processor else ''}"
        )
        return details_msg

    def shutdown(self) -> None:
        """Override shutdown to force flush first"""
        # Force flush to ensure all processors export their spans
        if hasattr(self, "force_flush"):
            try:
                self.force_flush(timeout_millis=5000)
            except Exception as e:
                print(f"Error during force flush: {e}")

        # Call the parent shutdown method
        super().shutdown()

    def setup_signal_handlers(self):
        """Set up signal handlers to ensure proper shutdown on termination"""

        def handle_signal(signum, frame):
            print(f"Received signal {signum}, shutting down tracer provider...")
            self.shutdown()
            sys.exit(0)

        # Register signal handlers
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        atexit.register(self.shutdown)

        return self


class SimpleSpanProcessor(_SimpleSpanProcessor):
    """
    Simple SpanProcessor implementation.

    SimpleSpanProcessor is an implementation of `SpanProcessor` that passes ended spans directly to
    the configured `SpanExporter`.

    Args:
        span_exporter (SpanExporter, optional): The `SpanExporter` to which ended spans will be
            passed.
        endpoint (str, optional): The collector endpoint to which spans will be exported. If not
            provided, the `BASE_URL` environment variable will be used to
            infer which collector endpoint to use, defaults to the gRPC endpoint. When specifying
            the endpoint, the transport method (HTTP or gRPC) will be inferred from the URL.
        headers (dict, optional): Optional headers to include in the request to the collector.
            If not provided, the `FI_API_KEY` and `FI_SECRET_KEY`
            environment variable will be used.
    """

    def __init__(
        self,
        span_exporter: Optional[SpanExporter] = None,
        headers: Optional[Dict[str, str]] = None,
        transport: Transport = Transport.HTTP,
    ):
        self._active_spans = {}

        if span_exporter is None:
            if transport == Transport.HTTP:
                endpoint = get_env_collector_endpoint()
                parsed_url, endpoint = _normalized_endpoint(endpoint)
                span_exporter = HTTPSpanExporter(endpoint=endpoint, headers=headers)
            elif transport == Transport.GRPC:
                endpoint = get_env_grpc_collector_endpoint()
                span_exporter = GRPCSpanExporter(endpoint=endpoint, headers=headers)

        super().__init__(span_exporter)

    def on_start(self, span: Any, parent_context: Optional[Any] = None) -> None:
        """Track span when it starts"""
        if hasattr(span, "context") and hasattr(span.context, "span_id"):
            max_active_spans_tracked = int(
                os.getenv(FI_MAX_ACTIVE_SPANS_TRACKED, DEFAULT_MAX_ACTIVE_SPANS_TRACKED)
            )
            if len(self._active_spans) >= max_active_spans_tracked:
                return
            self._active_spans[span.context.span_id] = span

        super().on_start(span, parent_context)

    def on_end(self, span: Any) -> None:
        """Remove span from tracking when it ends naturally"""
        if hasattr(span, "context") and hasattr(span.context, "span_id"):
            self._active_spans.pop(span.context.span_id, None)

        super().on_end(span)

    def shutdown(self) -> None:
        """Override shutdown to ensure all active spans get exported"""
        try:
            # Process any spans that haven't been ended
            if self._active_spans:
                print(f"Ending {len(self._active_spans)} active spans during shutdown")

                # Create a copy to avoid modification during iteration
                active_spans = list(self._active_spans.values())

                # End all active spans and mark them as leaked
                for span in active_spans:
                    if hasattr(span, "is_recording") and span.is_recording():
                        try:
                            # Mark the span as leaked
                            span.set_attribute("fi.span.leaked", True)
                            span.end()
                        except Exception as e:
                            pass

                # Clear the tracking dictionary
                self._active_spans.clear()
        finally:
            # Call the parent shutdown method
            super().shutdown()


class BatchSpanProcessor(_BatchSpanProcessor):
    """
    Batch SpanProcessor implementation.

    `BatchSpanProcessor` is an implementation of `SpanProcessor` that batches ended spans and
    pushes them to the configured `SpanExporter`.

    `BatchSpanProcessor` is configurable with the following environment variables which correspond
    to constructor parameters:

    - :envvar:`OTEL_BSP_SCHEDULE_DELAY`
    - :envvar:`OTEL_BSP_MAX_QUEUE_SIZE`
    - :envvar:`OTEL_BSP_MAX_EXPORT_BATCH_SIZE`
    - :envvar:`OTEL_BSP_EXPORT_TIMEOUT`

    Args:
        span_exporter (SpanExporter, optional): The `SpanExporter` to which ended spans will be
            passed.
        endpoint (str, optional): The collector endpoint to which spans will be exported. If not
            provided, the `BASE_URL` environment variable will be used to
            infer which collector endpoint to use, defaults to the gRPC endpoint. When specifying
            the endpoint, the transport method (HTTP or gRPC) will be inferred from the URL.
        headers (dict, optional): Optional headers to include in the request to the collector.
            If not provided, the `FI_API_KEY` and `FI_SECRET_KEY`
            environment variable will be used.
        max_queue_size (int, optional): The maximum queue size.
        schedule_delay_millis (float, optional): The delay between two consecutive exports in
            milliseconds.
        max_export_batch_size (int, optional): The maximum batch size.
        export_timeout_millis (float, optional): The batch timeout in milliseconds.
    """

    def __init__(
        self,
        span_exporter: Optional[SpanExporter] = None,
        headers: Optional[Dict[str, str]] = None,
        transport: Transport = Transport.HTTP,
    ):
        if span_exporter is None:
            if transport == Transport.HTTP:
                endpoint = get_env_collector_endpoint()
                parsed_url, endpoint = _normalized_endpoint(endpoint)
                span_exporter = HTTPSpanExporter(endpoint=endpoint, headers=headers)
            elif transport == Transport.GRPC:
                endpoint = get_env_grpc_collector_endpoint()
                span_exporter = GRPCSpanExporter(endpoint=endpoint, headers=headers)

        super().__init__(span_exporter)


class GRPCSpanExporter(_GRPCSpanExporter):
    """
    OTLP span exporter using gRPC.
    Args:
        endpoint (str, optional): OpenTelemetry Collector receiver endpoint. If not provided, the
            `BASE_URL` environment variable will be used to infer which
            collector endpoint to use, defaults to the gRPC endpoint.
        headers: Headers to send when exporting. If not provided, the `FI_API_KEY`
            and `FI_SECRET_KEY` environment variables will be used.
    """

    def __init__(self, *args: Any, **kwargs: Any):
        sig = _get_class_signature(_GRPCSpanExporter)
        bound_args = sig.bind_partial(*args, **kwargs)
        bound_args.apply_defaults()

        auth_header = get_env_fi_auth_header()
        lower_case_auth_header = (
            {k.lower(): v for k, v in auth_header.items()} if auth_header else None
        )

        if not bound_args.arguments.get("headers"):
            headers = {
                **(lower_case_auth_header or dict()),
            }
            bound_args.arguments["headers"] = headers if headers else None
        else:
            passed_headers = bound_args.arguments["headers"]
            if isinstance(passed_headers, dict):
                headers = {k.lower(): v for k, v in passed_headers.items()}
            else:
                headers = {k.lower(): v for k, v in passed_headers}

            if AUTHORIZATION not in headers:
                bound_args.arguments["headers"] = {
                    **headers,
                    **(lower_case_auth_header or dict()),
                }
            else:
                bound_args.arguments["headers"] = headers

        endpoint = get_env_grpc_collector_endpoint()
        bound_args.arguments["endpoint"] = endpoint
        super().__init__(*bound_args.args, **bound_args.kwargs)

    def shutdown(self) -> None:
        """Clean up any resources before shutting down."""
        try:
            if hasattr(self, "_session") and self._session:
                self._session.close()
        except Exception as e:
            print(f"Error during shutdown: {e}")


class HTTPSpanExporter(_HTTPSpanExporter):
    """
    OTLP span exporter using HTTP.

    For more information, see:
    - `opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter`

    Args:
        endpoint (str, optional): OpenTelemetry Collector receiver endpoint. If not provided, the
            `BASE_URL` environment variable will be used to infer which
            collector endpoint to use, defaults to the HTTP endpoint.
        headers: Headers to send when exporting. If not provided, the `FI_API_KEY`
            and `FI_SECRET_KEY` environment variables will be used.
    """

    def __init__(self, *args: Any, **kwargs: Any):
        sig = _get_class_signature(_HTTPSpanExporter)
        bound_args = sig.bind_partial(*args, **kwargs)
        bound_args.apply_defaults()

        if not bound_args.arguments.get("headers"):
            auth_header = get_env_fi_auth_header()
            headers = {
                **(auth_header or dict()),
            }
            bound_args.arguments["headers"] = headers if headers else None
        else:
            headers = dict()
            for header_field, value in bound_args.arguments["headers"].items():
                headers[header_field.lower()] = value

            # If the auth header is not in the headers, add it
            if AUTHORIZATION not in headers:
                auth_header = get_env_fi_auth_header()
                bound_args.arguments["headers"] = {
                    **headers,
                    **(auth_header or dict()),
                }
            else:
                bound_args.arguments["headers"] = headers

        if bound_args.arguments.get("endpoint") is None:
            _, endpoint = _normalized_endpoint(None)
            bound_args.arguments["endpoint"] = endpoint
        super().__init__(*bound_args.args, **bound_args.kwargs)

    def _convert_attributes(self, attributes):
        """Convert mappingproxy objects to regular dictionaries."""
        if attributes is None:
            return {}
        if not isinstance(attributes, dict):
            return dict(attributes)
        return attributes

    def _format_trace_id(self, trace_id: int) -> str:
        # Format the trace_id as a 32-character hexadecimal UUID
        return f"{trace_id:032x}"

    def _format_span_id(self, span_id: int) -> str:
        # Format the span_id as a 16-character hexadecimal
        return f"{span_id:016x}"

    def export(self, spans) -> SpanExportResult:
        """
        Exports a batch of spans in JSON format.
        Args:
            spans (list): A list of spans to export.
        Returns:
            SpanExportResult: Indicates the success or failure of the export.
        """
        try:
            # Serialize spans to JSON with converted attributes
            spans_data = []
            for span in spans:
                span_data = {
                    "trace_id": self._format_trace_id(span.context.trace_id),
                    "span_id": self._format_span_id(span.context.span_id),
                    "name": span.name,
                    "start_time": span.start_time,
                    "end_time": span.end_time,
                    "attributes": self._convert_attributes(span.attributes),
                    "events": [
                        {
                            "name": event.name,
                            "attributes": self._convert_attributes(event.attributes),
                            "timestamp": event.timestamp,
                        }
                        for event in span.events
                    ],
                    "status": span.status.status_code.name,
                    "parent_id": (
                        self._format_span_id(span.parent.span_id)
                        if span.parent
                        else None
                    ),
                    "project_name": span.resource.attributes.get(PROJECT_NAME),
                    "project_type": span.resource.attributes.get(PROJECT_TYPE),
                    "project_version_name": span.resource.attributes.get(
                        PROJECT_VERSION_NAME
                    ),
                    "project_version_id": span.resource.attributes.get(
                        PROJECT_VERSION_ID
                    ),
                    "latency": math.floor((span.end_time - span.start_time) / 1000000),
                    "eval_tags": span.resource.attributes.get(EVAL_TAGS),
                    "metadata": span.resource.attributes.get(METADATA),
                    "session_name": span.resource.attributes.get(SESSION_NAME),
                }

                spans_data.append(span_data)

            # Send data to the endpoint

            response = requests.post(
                self._endpoint,
                headers=self._headers,
                json=spans_data,  # Send JSON payload
            )
            response.raise_for_status()
            return SpanExportResult.SUCCESS

        except requests.Timeout as e:
            print(f"Failed to export spans due to timeout: {e}")
            return SpanExportResult.FAILURE
        except requests.HTTPError as e:
            error_log = f"Failed to export spans due to an HTTP error: {e}"

            if e.response is not None:
                try:
                    response_data = e.response.json()           
                    if isinstance(response_data, dict) and 'result' in response_data:
                        server_error = response_data['result']
                        error_log = f"Failed to export spans due to an error. Server responded with: {server_error}"
                except ValueError:
                    pass

            print(error_log)
            return SpanExportResult.FAILURE
        except Exception as e:
            print(f"Failed to export spans due to an unexpected error: {e}")
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        """Clean up any resources before shutting down."""
        try:
            if hasattr(self, "_session") and self._session:
                self._session.close()
        except Exception as e:
            print(f"Error during shutdown: {e}")


def _exporter_transport(exporter: SpanExporter) -> str:
    if isinstance(exporter, _HTTPSpanExporter):
        return "HTTP"
    if isinstance(exporter, _GRPCSpanExporter):
        return "gRPC"
    else:
        return exporter.__class__.__name__


def _printable_headers(
    headers: Union[List[Tuple[str, str]], Dict[str, str]]
) -> Dict[str, str]:
    if isinstance(headers, dict):
        return {key: "****" for key, _ in headers.items()}
    return {key: "****" for key, _ in headers}


def _construct_http_endpoint(parsed_endpoint: ParseResult) -> ParseResult:
    return parsed_endpoint._replace(path="/tracer/observation-span/create_otel_span/")


def _normalized_endpoint(endpoint: Optional[str]) -> Tuple[ParseResult, str]:
    if endpoint is None:
        endpoint = get_env_collector_endpoint()

    parsed = urlparse(endpoint)
    parsed = _construct_http_endpoint(parsed)

    return parsed, parsed.geturl()


def _get_class_signature(fn: Type[Any]) -> inspect.Signature:
    if sys.version_info >= (3, 9):
        return inspect.signature(fn)
    elif sys.version_info >= (3, 8):
        init_signature = inspect.signature(fn.__init__)
        new_params = list(init_signature.parameters.values())[1:]  # Skip 'self'
        new_sig = init_signature.replace(parameters=new_params)
        return new_sig
    else:
        raise RuntimeError("Unsupported Python version")


def check_custom_eval_config_exists(
    project_name: str, eval_tags: list, base_url: Optional[str] = None
) -> bool:
    """
    Check if a custom eval config exists for a given project.
    """
    if not eval_tags:
        return False

    if base_url is None:
        base_url = get_env_collector_endpoint()

    url = f"{base_url}/tracer/custom-eval-config/check_exists/"

    try:
        headers = {
            CONTENT_TYPE: "application/json",
            **(get_env_fi_auth_header() or {}),
        }

        response = requests.post(
            url,
            headers=headers,
            json={"project_name": project_name, "eval_tags": eval_tags},
        )

        response.raise_for_status()
        return response.json().get("result", {}).get("exists", False)

    except Exception as e:
        print(f"Failed to check custom eval config: {e}")
        return False
