"""Trial envelopes around the upstream converter, not another ATIF converter."""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone

import harbor_atif2otel
from harbor.models.trial.result import TrialResult
from harbor.models.trajectories import Trajectory
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import (
    AnyValue,
    InstrumentationScope,
    KeyValue,
)
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import (
    ResourceSpans,
    ScopeSpans,
    Span,
    Status,
)

from ._artifacts import ArtifactError, component, parse, read_bytes
from ._privacy import Privacy
from ._transport import Settings
from .version import __version__

_NUMERIC = {
    "llm.token_count.prompt",
    "llm.token_count.completion",
    "llm.token_count.total",
    "llm.token_count.prompt_details.cache_read",
    "llm.cost.total",
    "metadata.aggregated_llm_calls",
}
_LABELS = {
    "llm.model_name",
    "tool.name",
    "agent.name",
    "agent.version",
    "atif.schema_version",
}


def identity(seed: str, size: int = 8) -> bytes:
    return hashlib.sha256(seed.encode()).digest()[:size]


def attr(key: str, value: str | int | float | bool) -> KeyValue:
    if isinstance(value, bool):
        v = AnyValue(bool_value=value)
    elif isinstance(value, int):
        v = AnyValue(int_value=value)
    elif isinstance(value, float):
        v = AnyValue(double_value=value)
    else:
        v = AnyValue(string_value=str(value))
    return KeyValue(key=key, value=v)


def nanos(value: datetime | None) -> int:
    if value is None:
        return 0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1e9)


def _converter_trajectory(trajectory):
    """Bridge Harbor's v1.8 text schema to the pinned native converter's v1.7.

    Harbor validates the full v1.8 document first. Audio is new in v1.8;
    refusing it prevents silently presenting an incomplete reconstruction as complete.
    All other converter inputs retain their original structure and values.
    """

    def compatible(value):
        if isinstance(value, dict):
            if value.get("type") == "audio":
                raise ArtifactError("unsupported_audio_trajectory")
            if value.get("schema_version") == "ATIF-v1.8":
                Trajectory.model_validate(value)
            converted = {key: compatible(item) for key, item in value.items()}
            if value.get("schema_version") == "ATIF-v1.8":
                converted["schema_version"] = "ATIF-v1.7"
            return converted
        if isinstance(value, list):
            return [compatible(item) for item in value]
        return value

    return compatible(trajectory)


def _trajectory_check(trajectory):
    """Budget the converter's expanded tree before executing it; never resolve refs."""
    budget = 0
    incomplete = False

    def visit(traj, depth):
        nonlocal budget, incomplete
        if (
            depth > 8
            or not isinstance(traj, dict)
            or harbor_atif2otel.validate_trajectory(traj)
        ):
            raise ArtifactError("unsupported_trajectory")
        # Native 0.1.1 falls back to re-emitting every step when ALL are copied.
        # That would account for historical calls again. Keep only the envelope.
        if all(step.get("is_copied_context") for step in traj["steps"]):
            raise ArtifactError("copied_context_only")
        children = {
            t["trajectory_id"]: t for t in traj.get("subagent_trajectories", [])
        }
        # Continued trajectories may refer to files or URLs. We deliberately do not fetch.
        if any("continu" in k.lower() and v for k, v in traj.items()):
            incomplete = True
        budget += 1 + 2 * len(traj["steps"])
        for step in traj["steps"]:
            budget += len(step.get("tool_calls", []))
            if budget > 4096:
                raise ArtifactError("trajectory_budget")
            for result in (step.get("observation") or {}).get("results", []):
                refs = result.get("subagent_trajectory_ref", [])
                if not isinstance(refs, list):
                    refs = [refs]
                for ref in refs:
                    key = ref.get("trajectory_id") if isinstance(ref, dict) else None
                    if key not in children:
                        incomplete = True
                    else:
                        visit(children[key], depth + 1)

    visit(trajectory, 0)
    return incomplete


def _result_attrs(result, privacy, prefix="harbor"):
    attrs = []
    verifier = result.verifier_result
    if verifier and verifier.rewards:
        for i, (name, value) in enumerate(verifier.rewards.items()):
            if i >= 128:
                raise ArtifactError("too_many_rewards")
            if math.isfinite(value):
                attrs.append(attr(f"{prefix}.reward.{privacy.text(name, 128)}", value))
    if result.exception_info:
        # Exception messages, tracebacks and arbitrary exception type strings are not exported.
        attrs.append(attr(f"{prefix}.exception", True))
    return attrs


def convert_trial(
    root_fd: int, trial_name: str, job_id: str, settings: Settings
) -> tuple[str, bytes, bool, str]:
    component(trial_name)
    raw = read_bytes(root_fd, (trial_name, "result.json"))
    fingerprint = hashlib.sha256(raw)
    data = parse(raw)
    if not isinstance(data, dict) or not data.get("id"):
        raise ArtifactError("missing_trial_identity")
    result = TrialResult.model_validate(data)
    if result.config.job_id is not None and str(result.config.job_id) != job_id:
        raise ArtifactError("mismatched_job_identity")
    if result.trial_name != trial_name or not result.finished_at:
        raise ArtifactError("unfinished_or_mismatched_trial")
    begin, end = nanos(result.started_at), nanos(result.finished_at)
    if not 0 < begin <= end < 2**63:
        raise ArtifactError("missing_trial_timing")
    privacy = Privacy(settings)
    seed = f"harbor:{job_id}:{result.id}"
    trace_id, root_id = identity(seed, 16), identity(seed + ":trial")
    session = str(result.id)
    attrs = [
        attr("fi.span.kind", "CHAIN"),
        attr("session.id", session),
        attr("harbor.job.id", job_id),
        attr("harbor.trial.id", session),
        attr("harbor.task.name", privacy.text(result.task_name, 256)),
        attr(
            "harbor.task.id",
            hashlib.sha256(result.task_id.model_dump_json().encode()).hexdigest(),
        ),
        attr("harbor.trial.name", privacy.text(result.trial_name, 256)),
        attr("harbor.task.checksum", privacy.text(result.task_checksum, 256)),
        attr("harbor.agent.name", privacy.text(result.agent_info.name, 256)),
        attr("harbor.reward_valid", result.reward_valid),
    ]
    if result.agent_info.model_info:
        attrs.append(
            attr(
                "harbor.model.name",
                privacy.text(result.agent_info.model_info.name, 256),
            )
        )
    if result.failure_cause:
        attrs.append(attr("harbor.failure_cause", result.failure_cause.value))
    attrs.extend(_result_attrs(result, privacy))
    for name, value in zip(
        ("input_tokens", "cache_tokens", "output_tokens", "cost_usd"),
        result.compute_token_cost_totals(),
    ):
        if value is not None and math.isfinite(value) and value >= 0:
            attrs.append(attr(f"harbor.summary.{name}", value))
    root = Span(
        trace_id=trace_id,
        span_id=root_id,
        name="Harbor trial",
        kind=Span.SPAN_KIND_INTERNAL,
        start_time_unix_nano=begin,
        end_time_unix_nano=end,
        attributes=attrs,
        status=Status(
            code=Status.STATUS_CODE_ERROR
            if result.failure_cause
            else Status.STATUS_CODE_UNSET
        ),
    )
    spans = [root]

    def phase(name, timing, parent=root_id, extra=()):
        if timing is None or not timing.started_at or not timing.finished_at:
            return parent
        start, finish = nanos(timing.started_at), nanos(timing.finished_at)
        if not begin <= start <= finish <= end:
            return parent
        sid = identity(seed + ":phase:" + name)
        spans.append(
            Span(
                trace_id=trace_id,
                span_id=sid,
                parent_span_id=parent,
                name="Harbor " + name.split(":")[-1],
                kind=Span.SPAN_KIND_INTERNAL,
                start_time_unix_nano=start,
                end_time_unix_nano=finish,
                attributes=[
                    attr("fi.span.kind", "CHAIN"),
                    attr("session.id", session),
                    *extra,
                ],
            )
        )
        return sid

    phase("environment_setup", result.environment_setup)
    phase("agent_setup", result.agent_setup)
    phase("verifier", result.verifier)
    execution_id = phase("agent_execution", result.agent_execution)
    trajectories = [("single", (trial_name, "agent", "trajectory.json"), execution_id)]
    if result.step_results:
        trajectories = []
        if len(result.step_results) > 128:
            raise ArtifactError("too_many_steps")
        for index, step in enumerate(result.step_results):
            component(step.step_name)
            extra = [
                attr("harbor.step.name", privacy.text(step.step_name, 256)),
                *_result_attrs(step, privacy),
            ]
            parent = phase(
                f"{index}:agent_execution", step.agent_execution, extra=extra
            )
            phase(f"{index}:verifier", step.verifier, extra=extra)
            # Preserve step results even when no phase timing was saved.
            root.attributes.extend(
                attr(
                    f"harbor.steps.{index}.{a.key}",
                    getattr(a.value, a.value.WhichOneof("value")),
                )
                for a in extra
            )
            trajectories.append(
                (
                    str(index),
                    (trial_name, "steps", step.step_name, "agent", "trajectory.json"),
                    parent,
                )
            )
    incomplete = False
    for part, path, parent in trajectories:
        try:
            raw_trajectory = read_bytes(root_fd, path)
            fingerprint.update(part.encode() + b"\0" + raw_trajectory)
            trajectory = parse(raw_trajectory)
            converter_input = _converter_trajectory(trajectory)
            incomplete |= _trajectory_check(converter_input)
            # Redact before the native converter truncates strings (partial secrets must not survive).
            native = harbor_atif2otel.convert_trajectory(
                privacy.tree(converter_input),
                trace_seed=seed + ":" + part,
                max_attribute_bytes=2048,
            )
            native_spans = [s for scope in native.scope_spans for s in scope.spans]
            if len(native_spans) + len(spans) > 8192:
                raise ArtifactError("span_budget")
            versions = {}

            def collect_versions(item):
                if item.get("trajectory_id"):
                    versions[item["trajectory_id"]] = item["schema_version"]
                for child in item.get("subagent_trajectories", []):
                    collect_versions(child)

            collect_versions(trajectory)
            ids = {s.span_id for s in native_spans}
            if len(ids) != len(native_spans) or any(
                not s.span_id or s.parent_span_id and s.parent_span_id not in ids
                for s in native_spans
            ):
                raise ArtifactError("invalid_parentage")
            for span in native_spans:
                original = {a.key: a.value for a in span.attributes}
                kind = original.get(
                    "openinference.span.kind", AnyValue(string_value="CHAIN")
                ).string_value
                if kind not in {"AGENT", "LLM", "TOOL", "CHAIN"}:
                    kind = "CHAIN"
                projected = [
                    attr("fi.span.kind", kind),
                    attr("session.id", session),
                    attr("harbor.reconstructed", True),
                    attr("harbor.timing.source", "atif_approximate"),
                ]
                for key, value in original.items():
                    field = value.WhichOneof("value")
                    if key in _NUMERIC and field in {"int_value", "double_value"}:
                        number = getattr(value, field)
                        if math.isfinite(number) and number >= 0:
                            accounting_key = (
                                key if kind == "LLM" else "harbor.summary." + key
                            )
                            projected.append(attr(accounting_key, number))
                    elif key in _LABELS and field == "string_value":
                        label = value.string_value
                        if key == "atif.schema_version":
                            trajectory_id = original.get("trajectory.id")
                            label = versions.get(
                                trajectory_id.string_value if trajectory_id else "",
                                trajectory["schema_version"]
                                if not span.parent_span_id
                                else value.string_value,
                            )
                        projected.append(attr(key, privacy.text(label, 256)))
                    elif key == "session.id" and field == "string_value":
                        projected.append(
                            attr(
                                "harbor.atif.session_id",
                                privacy.text(value.string_value, 256),
                            )
                        )
                    elif (
                        settings.capture_content
                        and key in {"input.value", "output.value"}
                        and field == "string_value"
                    ):
                        projected.append(attr(key, privacy.text(value.string_value)))
                if kind == "LLM" and "llm.token_count.total" not in original:
                    prompt = original.get("llm.token_count.prompt")
                    completion = original.get("llm.token_count.completion")
                    if prompt is not None and completion is not None:
                        counts = (prompt, completion)
                        if all(
                            v.WhichOneof("value") == "int_value" and v.int_value >= 0
                            for v in counts
                        ):
                            projected.append(
                                attr(
                                    "llm.token_count.total",
                                    prompt.int_value + completion.int_value,
                                )
                            )
                span.ClearField("attributes")
                span.attributes.extend(projected)
                span.ClearField("events")
                span.ClearField("links")
                span.ClearField(
                    "status"
                )  # Artifact conversion does not prove provider success.
                span.name = "ATIF " + kind.lower()
                span.trace_id = trace_id
                if not span.parent_span_id:
                    span.parent_span_id = parent
                if (
                    not begin
                    <= span.start_time_unix_nano
                    <= span.end_time_unix_nano
                    <= end
                ):
                    span.start_time_unix_nano, span.end_time_unix_nano = begin, end
                    span.attributes.append(
                        attr("harbor.timing.fallback", "trial_bounds")
                    )
                spans.append(span)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            OverflowError,
            RecursionError,
        ):
            fingerprint.update(part.encode() + b"\0unavailable")
            incomplete = True
    root.attributes.append(attr("harbor.trace.incomplete", incomplete))
    resource = Resource(
        attributes=[
            attr("service.name", "harbor"),
            attr("project_name", privacy.text(settings.project_name, 256)),
            attr("project_type", "observe"),
            attr("telemetry.sdk.language", "python"),
        ]
    )
    request = ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=resource,
                scope_spans=[
                    ScopeSpans(
                        scope=InstrumentationScope(
                            name="traceai-harbor", version=__version__
                        ),
                        spans=spans,
                    )
                ],
            )
        ]
    )
    payload = request.SerializeToString(deterministic=True)
    if len(payload) > 8 * 1024 * 1024:
        raise ArtifactError("payload_size")
    fingerprint.update(payload)
    return str(result.id), payload, incomplete, fingerprint.hexdigest()
