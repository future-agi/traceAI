"""Bounded, response-only recording metadata and FI-export-copy aliases.

No LiveKit runtime imports, request serialization, media IO, or credential capture.
URL projection is deliberately stricter than the generic FI value mask: retained
locators are not authenticated playback links.
"""

import ipaddress
import re
from itertools import islice
from typing import Any, Dict, Mapping, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit

from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from fi_instrumentation.instrumentation.config import REDACTED_VALUE


_OPERATIONS = frozenset((
    "start_room_composite_egress", "start_web_egress", "start_participant_egress",
    "start_track_composite_egress", "start_track_egress", "stop_egress",
    "update_stream", "update_layout",
))
_STATUS_NAMES = frozenset((
    "EGRESS_STARTING", "EGRESS_ACTIVE", "EGRESS_ENDING", "EGRESS_COMPLETE",
    "EGRESS_FAILED", "EGRESS_ABORTED", "EGRESS_LIMIT_REACHED",
))
_MARKER = "lk.recording.operation"
_RECORDING_SPAN_NAMES = frozenset("livekit.recording." + op for op in (*_OPERATIONS, "snapshot"))
_ALIASES = {
    _MARKER: "livekit.recording.operation",
    "lk.pii.recording.egress_id": "livekit.recording.egress_id",
    "lk.recording.status": "livekit.recording.status",
    "lk.recording.started_at_unix_nano": "livekit.recording.started_at_unix_nano",
    "lk.recording.ended_at_unix_nano": "livekit.recording.ended_at_unix_nano",
    "lk.pii.room_name": "livekit.recording.room_name",
    "lk.pii.recording.conversation_id": SpanAttributes.GEN_AI_CONVERSATION_ID,
    "lk.pii.recording.file_locations": "livekit.recording.file_locations",
    "lk.pii.recording.stream_locations": "livekit.recording.stream_locations",
}
_LOCATION_KEYS = frozenset((
    "lk.pii.recording.file_locations", "lk.pii.recording.stream_locations",
))
_MAX_ITEMS = 8
_MAX_TEXT = 256
_MAX_LOCATION = 2048
_INT64_MAX = (1 << 63) - 1
_UNSAFE_URL_TEXT = re.compile(r"[\x00-\x20\x7f\\]|%(?:0[0-9a-f]|1[0-9a-f]|7f|5c|25)", re.I)


def _operation(value: Any) -> str:
    return value if type(value) is str and len(value) <= 64 and value in _OPERATIONS else "snapshot"


def _text(value: Any, limit: int = _MAX_TEXT) -> Optional[str]:
    # Do not truncate: truncation can silently identify the wrong room or object.
    if type(value) is str and 0 < len(value) <= limit and value.strip():
        return value
    return None


def _sanitize_recording_location(value: Any, *, stream: bool = False) -> Optional[str]:
    """Drop unsafe locations; remove *all* userinfo/query/fragment data.

    File URIs retain their path; stream paths may be bearer keys and never survive.
    Relative names are a deliberately small lexical subset, never filesystem reads.
    This is a conservative fallback, not a claim of a shared URL sanitizer API.
    """
    value = _text(value, _MAX_LOCATION)
    if value is None or not value.isascii() or _UNSAFE_URL_TEXT.search(value):
        return None
    try:
        parts = urlsplit(value)
        schemes = ("https", "http", "rtmp", "rtmps", "rtsp", "rtsps") if stream else ("https", "http", "s3", "gs")
        if not parts.scheme:
            if stream or parts.netloc or value.startswith("/"):
                return None
            path = parts.path
            if not path or not re.fullmatch(r"[A-Za-z0-9_.\-/]+", path):
                return None
            if any(segment in ("", ".", "..") for segment in path.split("/")):
                return None
            return path
        if parts.scheme not in schemes or not parts.netloc:
            return None
        host = parts.hostname
        if not host or "%" in host:  # Reject IPv6 zone ids and encoded authorities.
            return None
        if ":" in host:
            ipaddress.IPv6Address(host)
            host = "[" + host + "]"
        elif not re.fullmatch(r"[A-Za-z0-9.-]+", host):
            return None
        port = parts.port  # Force validation; invalid ports are not safe locators.
        authority = host + ((":" + str(port)) if port is not None else "")
        path = "" if stream else parts.path
        return urlunsplit((parts.scheme, authority, path, "", ""))
    except ValueError:
        return None


def _locations(info: Any, collection: str, *, stream: bool = False) -> tuple:
    results = getattr(info, collection, None)
    if results is None:
        return ()
    retained = []
    # Bound examined elements, not just accepted results. Never consume item nine.
    for result in islice(iter(results), _MAX_ITEMS):
        candidate = getattr(result, "url" if stream else "location", None)
        if not stream and (candidate is None or (type(candidate) is str and candidate == "")):
            candidate = _text(getattr(result, "filename", None), _MAX_LOCATION)
            # Filename fallback is relative-only, not another URI or absolute path.
            if candidate is None or ":" in candidate or candidate.startswith("/"):
                continue
        safe = _sanitize_recording_location(candidate, stream=stream)
        if safe is not None:
            retained.append(safe)
    return tuple(retained)


def _mask_value(source: str, value: Any, policies: Sequence[TraceConfig]) -> Any:
    # Transform monotonically: never recover the original after a mask withholds it.
    for policy in policies:
        keys = (SpanAttributes.OUTPUT_VALUE, _ALIASES[source], source) if source in _LOCATION_KEYS else (_ALIASES[source], source)
        for key in keys:
            value = policy.mask(key, value)
            if value is None:
                return None
    return value


def _recording_span_attributes(
    info: Any, *, operation: str, room_name: Optional[str],
    conversation_id: Optional[str], policies: Sequence[TraceConfig],
) -> Optional[Dict[str, Any]]:
    """Extract a complete safe snapshot before any sampler/processor can observe it.

    The EgressInfo-like contract uses positive integer Unix nanoseconds for the two
    time fields. No enum-number meaning, time conversion or completion is inferred.
    """
    egress_id = _text(getattr(info, "egress_id", None))
    if egress_id is None:
        return None
    reported_room = getattr(info, "room_name", None)
    response_room = _text(reported_room)
    if room_name is not None:
        if _text(room_name) is None:
            return None
        # An oversize/invalid nonempty room is not evidence of a matching room.
        if reported_room is not None and not (type(reported_room) is str and reported_room == ""):
            if response_room is None or response_room != room_name:
                return None
        room = room_name
    else:
        room = response_room
    if conversation_id is not None and _text(conversation_id) is None:
        return None
    values: Dict[str, Any] = {_MARKER: operation, "lk.pii.recording.egress_id": egress_id}
    if room is not None:
        values["lk.pii.room_name"] = room
    # Keep this after the room: an implicit conversation is resolved from its
    # fully masked, validated source in the loop, never copied from the raw room.
    values["lk.pii.recording.conversation_id"] = conversation_id
    status = getattr(info, "status", None)
    if type(status) is str and len(status) <= 64 and status in _STATUS_NAMES:
        values["lk.recording.status"] = status
    elif type(status) is int and 0 <= status <= (1 << 31) - 1:
        values["lk.recording.status"] = str(status)
    for field in ("started_at", "ended_at"):
        timestamp = getattr(info, field, None)
        if type(timestamp) is int and 0 < timestamp <= _INT64_MAX:
            values["lk.recording." + field + "_unix_nano"] = timestamp
    for collection, stream in (("file_results", False), ("stream_results", True)):
        locations = _locations(info, collection, stream=stream)
        if locations:
            values["lk.pii.recording." + ("stream" if stream else "file") + "_locations"] = locations
    safe: Dict[str, Any] = {}
    for source, value in values.items():
        if source == "lk.pii.recording.conversation_id" and conversation_id is None:
            value = safe.get("lk.pii.room_name")
        if value is None:
            continue
        masked = _mask_value(source, value, policies)
        if masked is None:
            continue
        if source in _LOCATION_KEYS:
            if type(masked) is str:
                masked = (masked,)  # A whole-value redaction remains a redaction.
            if type(masked) not in (tuple, list) or not 0 < len(masked) <= _MAX_ITEMS:
                continue
            if any(_text(item, _MAX_LOCATION) is None for item in masked):
                continue
            masked = tuple(masked)
        elif type(masked) is str:
            if _text(masked) is None:
                continue
        elif type(masked) is not int or not 0 < masked <= _INT64_MAX:
            continue
        if source == _MARKER and type(masked) is not str:
            continue
        safe[source] = masked
    # Preserve the explicit policy opt-out for recording operation metadata.
    # Export classification does not rely on this attribute surviving the SDK.
    if _MARKER not in safe:
        return None
    return safe


def _is_recording_snapshot(
    attributes: Mapping[str, Any], *, span_name: Optional[str] = None,
) -> bool:
    # The exact helper-controlled names survive sampler selection and SDK limits.
    # Tracers supplied by callers have arbitrary scopes: conservatively reserve
    # these names across scopes rather than unwrap or replace their tracer.
    # Marker presence remains useful for attribute-only callers/older snapshots.
    return _MARKER in attributes or span_name in _RECORDING_SPAN_NAMES


def _map_recording_attributes(
    attributes: Mapping[str, Any], mapped: Dict[str, Any], *, recording_snapshot: bool = False,
) -> None:
    """Project retained source names; the exporter separately rechecks policy."""
    if not recording_snapshot and not _is_recording_snapshot(attributes):
        return
    for source, alias in _ALIASES.items():
        # Never restore an alias after its source was omitted or redacted by
        # LiveKit. Source presence controls projection, not writer provenance.
        mapped.pop(alias, None)
        if source in attributes:
            mapped[alias] = attributes[source]


def _withhold_recording_failures(
    attributes: Mapping[str, Any], failed_sources: set,
) -> Dict[str, Any]:
    """Finalize an export copy without invoking a failed policy again."""
    checked = dict(attributes)
    for source, alias in _ALIASES.items():
        if source in failed_sources:
            value = (REDACTED_VALUE,) if source in _LOCATION_KEYS else REDACTED_VALUE
            for key in (source, alias):
                if key in checked:
                    checked[key] = value
    if SpanAttributes.SESSION_ID in failed_sources and SpanAttributes.SESSION_ID in checked:
        checked[SpanAttributes.SESSION_ID] = REDACTED_VALUE
    return checked


def _recheck_recording_attributes(
    attributes: Mapping[str, Any], policies: Sequence[TraceConfig], *,
    failed_sources: Optional[set] = None,
) -> Dict[str, Any]:
    """Reapply recording policy by reserved key name, irrespective of writer.

    Used on export-copy span, event and link attribute dictionaries. Unrelated
    keys are preserved. A policy Exception is a withheld value, never a reason
    to export the original or include exception text in telemetry.
    """
    checked = dict(attributes)
    if failed_sources is None:
        failed_sources = set()
    for source, alias in _ALIASES.items():
        keys = tuple(key for key in (source, alias) if key in attributes)
        if not keys:
            continue
        values = {}
        # The shared set contains only reserved key names for this export copy,
        # never values or provenance. A failed family must not retry raw data
        # in another span/event/link dictionary after a transient recovery.
        failed = source in failed_sources
        if not failed:
            try:
                for key in keys:
                    if key == alias and source in values and attributes[alias] == attributes[source]:
                        # A projected duplicate inherits the already-checked
                        # result. Never retry the raw source through its alias.
                        values[key] = values[source]
                    else:
                        values[key] = _mask_value(source, attributes[key], policies)
            except Exception:
                failed = True
        if failed:
            # Commit a family only after all of its gates succeed. This also
            # covers distinct application-written reserved aliases and either
            # input ordering, without affecting unrelated application keys.
            values = dict.fromkeys(keys, REDACTED_VALUE)
            failed_sources.add(source)
            if source == "lk.pii.room_name":
                # Implicit conversations derive from the room. Conservatively
                # withhold that reserved family too, without tracking writers.
                failed_sources.add("lk.pii.recording.conversation_id")
        for key, value in values.items():
            if value is None:
                checked.pop(key, None)
            else:
                if source in _LOCATION_KEYS and type(value) is str:
                    value = (value,)
                checked[key] = value

    key = SpanAttributes.SESSION_ID
    if key in attributes:
        value = REDACTED_VALUE if key in failed_sources else attributes[key]
        if key not in failed_sources:
            try:
                for policy in policies:
                    value = policy.mask(key, value)
                    if value is None:
                        break
            except Exception:
                failed_sources.add(key)
                value = REDACTED_VALUE
        if value is None:
            checked.pop(key, None)
        else:
            checked[key] = value
    return checked
