"""Offline fixture validators for fi.attachments.v1; not a runtime SDK or server.

Consumers load the JSON fixtures directly. This executable reference oracle never
reads a media source, sends a request, accesses credentials, or retrieves schemas.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError as jsonschema_error

ROOT = Path(__file__).resolve().parent


def load(name: str) -> Any:
    """Read only this checked-in contract's data files."""
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


SCHEMA = load("contract.schema.json")


def validator(name: str) -> Draft202012Validator:
    """Use local definitions; never resolve a network schema."""
    return Draft202012Validator(
        {"$schema": SCHEMA["$schema"], "$defs": SCHEMA["$defs"], "$ref": "#/$defs/" + name},
        format_checker=FormatChecker(),
    )


def normalize_trace(value: str) -> str:
    """Validate before conversion; preserve all 128 bits including UUID variant."""
    validator("TraceInput").validate(value)
    return str(UUID(hex=value.replace("-", "")))


def decide(table: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Interpret the ordered JSON Schema predicates in a conformance table."""
    if not Draft202012Validator(table["context_schema"]).is_valid(context):
        return {"decision": "deny", "reason": "policy_denied", "effects": []}
    for rule in table["rules"]:
        if Draft202012Validator(rule["deny_if"]).is_valid(context):
            return {"decision": "deny", "reason": rule["reason"], "effects": []}
    return copy.deepcopy(table["allow"])


def strict_json(raw: str) -> Any:
    """Duplicate keys and non-JSON numbers must not hide a conflicting request."""
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON member")
            result[key] = value
        return result

    def invalid_constant(_value: str) -> None:
        raise ValueError("non-JSON number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)


def validate_wire(name: str, value: Any) -> None:
    """Enforce JSON Schema plus serialization and cross-field invariants."""
    validator(name).validate(value)
    if name == "SpanAttributes":
        raw = value["fi.attachment.refs"]
        if len(raw.encode("utf-8")) > 16384:
            raise ValueError("reference byte limit")
        validate_wire("References", strict_json(raw))
    if name == "References":
        ids = [entry["occurrence_id"] for entry in value]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate occurrence projection")
        for entry in value:
            validate_wire("Reference", entry)
    if name in {"Reference", "UploadRequest", "MessageURI"}:
        modality, mime = value["modality"], value["mime_type"]
        allowed = {
            "audio": mime.startswith("audio/"), "image": mime.startswith("image/"),
            "video": mime.startswith("video/"), "document": mime == "application/pdf",
            "file": mime == "application/octet-stream",
        }
        if not allowed[modality]:
            raise ValueError("modality/MIME mismatch")
    if name == "UploadRequest":
        if value["modality"] == "image" and value["declared_bytes"] > 20971520:
            raise ValueError("image byte limit")
        if "recording_id" in value and value["declared_bytes"] > 8388608:
            raise ValueError("recording segment byte limit")
    if name == "Occurrence":
        validate_wire("Reference", value["reference"])
    if name == "OccurrencePage":
        for occurrence in value["items"]:
            validate_wire("Occurrence", occurrence)
    if name == "ResolveResponse":
        for item in value["items"]:
            if item["status"] == "ready" and item["gateway_path"].split("/")[4] != item["occurrence_id"]:
                raise ValueError("gateway occurrence mismatch")
    if name in {"ImportRequest", "SingleAttachmentImportRequest", "RecordingImportRequest"}:
        recording = value.get("mode") == "completed_recording"
        descriptor = value["recording" if recording else "upload"]
        validate_wire("CompletedRecordingImport" if recording else "UploadRequest", descriptor)
        expected = "RemoteUrl" if value["source"]["kind"] == "RemoteUrlEnvelope" else "ProviderFile"
        if descriptor["source_kind"] != expected:
            raise ValueError("import source mismatch")
    if name == "RecordingManifest":
        if len(json.dumps(value, separators=(",", ":")).encode("utf-8")) > 1048576:
            raise ValueError("manifest byte limit")
        if value["capture_complete"] and value["gaps"]:
            raise ValueError("complete recording has capture gaps")
        track_ids = [track["track_id"] for track in value["tracks"]]
        if len(track_ids) != len(set(track_ids)):
            raise ValueError("duplicate track")
        channels = [channel for track in value["tracks"] for channel in track["channels"]]
        if sorted(channels) != list(range(value["channels"])):
            raise ValueError("channel mapping must cover each channel exactly once")
        if value["layout"] == "mixed" and any(track["direction"] != "mixed" for track in value["tracks"]):
            raise ValueError("mixed recording cannot claim isolated directions")
        if value["layout"] == "stereo" and value["channels"] != 2:
            raise ValueError("stereo needs two channels")
        seen = set()
        previous_start = -1
        for index, segment in enumerate(value["segments"]):
            if segment["index"] != index or segment["start_ms"] < previous_start or segment["occurrence_id"] in seen:
                raise ValueError("unordered or duplicate segment")
            seen.add(segment["occurrence_id"])
            previous_start = segment["start_ms"]
        timelines = {track: [] for track in track_ids} if value["layout"] == "separate_tracks" else {None: []}
        for interval in value["segments"] + value["gaps"]:
            track = interval.get("track_id")
            if track not in timelines:
                raise ValueError("missing or unrelated track identity")
            timelines[track].append(interval)
        for intervals in timelines.values():
            end = 0
            for interval in sorted(intervals, key=lambda item: item["start_ms"]):
                if interval["start_ms"] != end:
                    raise ValueError("overlap or unmarked capture gap")
                end += interval["duration_ms"]
            if end != value["duration_ms"]:
                raise ValueError("missing recording coverage")
        for interval in value["segments"] + value["gaps"] + value["playout"]:
            if interval["start_ms"] + interval["duration_ms"] > value["duration_ms"]:
                raise ValueError("interval exceeds recording")


def fixture_override(base: Any, overrides: Any) -> Any:
    """Recursive object overrides; arrays/scalars replace. Fixture data only."""
    if isinstance(base, dict) and isinstance(overrides, dict):
        result = copy.deepcopy(base)
        for key, value in overrides.items():
            result[key] = fixture_override(result.get(key), value)
        return result
    return copy.deepcopy(overrides)


def import_window(limits: dict[str, Any]) -> int:
    return min(limits["max_upload_bytes"], limits["segment_bytes"])


def validate_import_admission(scenario: dict[str, Any]) -> None:
    """Test-only size/reservation oracle, not authorization or a source reader.

    The source facts and headroom are trusted server observations in fixtures,
    never client assertions. Privacy/security conformance remains independently
    required. Byte reservations cover the complete immutable snapshot PLUS one
    output window; completed sources do not get an unreserved streaming bypass.
    """
    validate_wire("RecordingImportRequest", scenario["request"])
    limits = scenario["limits"]
    validate_wire("Limits", limits)
    recording = scenario["request"]["recording"]
    if (recording["declared_bytes"] > limits["recording_bytes"] or
            recording["declared_duration_ms"] > limits["recording_duration_ms"]):
        raise ValueError("recording budget")
    if scenario["source"] != {"terminal_completed": True, "immutable": True, "bounded_ranges": True}:
        raise ValueError("unsupported completed source")
    reservation = recording["declared_bytes"] + import_window(limits)
    if reservation > limits["reserved_bytes"]:
        raise ValueError("project reservation budget")
    required = {"bytes": reservation, "transfers": 1, "recordings": 1,
                "validation_jobs": 1, "objects": 2, "links": 1}
    if set(scenario["headroom"]) != {"project", "organization", "global"}:
        raise ValueError("unknown admission capacity")
    for available in scenario["headroom"].values():
        if set(available) != set(required):
            raise ValueError("unknown resource capacity")
        for resource, minimum in required.items():
            if type(available[resource]) is not int or available[resource] < minimum:
                raise ValueError("aggregate reservation budget")


def validate_import_completion(scenario: dict[str, Any]) -> None:
    """Validate synthetic worker observations; never decode or retrieve media."""
    validate_wire("RecordingImportRequest", scenario["request"])
    limits = scenario["limits"]
    validate_wire("Limits", limits)
    recording = scenario["request"]["recording"]
    observed = scenario["observation"]
    if (observed["source_bytes"] != recording["declared_bytes"] or
            observed["source_bytes"] > limits["recording_bytes"] or
            observed["source_duration_ms"] != recording["declared_duration_ms"] or
            observed["source_duration_ms"] > limits["recording_duration_ms"] or
            observed["generation_matches"] is not True):
        raise ValueError("source length, duration or generation mismatch")
    end = 0
    window = import_window(limits)
    for part in observed["reads"]:
        if (part["offset"] != end or type(part["bytes"]) is not int or
                not 0 < part["bytes"] <= window or
                part["total_bytes"] != recording["declared_bytes"]):
            raise ValueError("source range mismatch")
        end += part["bytes"]
    if end != recording["declared_bytes"]:
        raise ValueError("incomplete or excess source ranges")
    manifest = observed["manifest"]
    validate_wire("RecordingManifest", manifest)
    if (manifest["recording_id"] != recording["recording_id"] or
            manifest["duration_ms"] != recording["declared_duration_ms"] or
            not manifest["capture_complete"] or not manifest["segments"]):
        raise ValueError("completed import manifest mismatch")
    if recording["direction"] != "mixed" and any(
            track["direction"] != recording["direction"] for track in manifest["tracks"]):
        raise ValueError("import direction understates source ancestry")
    sizes = observed["segment_bytes"]
    if len(sizes) != len(manifest["segments"]):
        raise ValueError("missing verified segment sizes")
    if any(type(size) is not int or not 0 < size <= window for size in sizes):
        raise ValueError("import segment byte limit")
    if sum(sizes) > limits["recording_bytes"]:
        raise ValueError("aggregate output recording byte limit")
    if any(segment["duration_ms"] > limits["segment_duration_ms"] for segment in manifest["segments"]):
        raise ValueError("import segment duration limit")


def scoped_key(scope: dict[str, Any], fields: list[str]) -> tuple[Any, ...]:
    """Fixture key oracle: normalize trace IDs, retain the complete scope."""
    normalized = {**scope, "trace_id": normalize_trace(scope["trace_id"])}
    return tuple(normalized[field] for field in fields)


def lifecycle_result(table: dict[str, Any], events: list[str]) -> tuple[list[bool], str]:
    """Interpret only the contract's transition fixtures (no jobs or storage)."""
    state = table["initial"]["state"]
    bound = False
    readable = []
    for event in events:
        if event == "bind":
            if state not in table["terminal"]:
                bound = True
        else:
            for transition in table["transitions"]:
                if transition["event"] == event and state in transition["from"]:
                    state = transition["to"]
                    break
        readable.append(state == "ready" and bound)
    return readable, state


def validate_all() -> dict[str, int]:
    """Fail nonzero on any changed expectation; report only fixture IDs/counts."""
    Draft202012Validator.check_schema(SCHEMA)
    counts = {}
    for prefix in ("privacy", "security"):
        table = load(prefix + "-table.json")
        Draft202012Validator.check_schema(table["context_schema"])
        for rule in table["rules"]:
            Draft202012Validator.check_schema(rule["deny_if"])
        cases = load(prefix + "-fixtures.json")["cases"]
        for case in cases:
            if decide(table, case["context"]) != case["expected"]:
                raise AssertionError(prefix + ": " + case["id"])
        counts[prefix] = len(cases)
    cases = load("wire-fixtures.json")["cases"]
    for case in cases:
        try:
            validate_wire(case["schema"], case["value"])
            valid = True
        except (ValueError, jsonschema_error):
            valid = False
        if valid != case["valid"]:
            raise AssertionError("wire: " + case["id"])
    counts["wire"] = len(cases)
    cases = load("identity-fixtures.json")["cases"]
    for index, case in enumerate(cases):
        if validator("TraceInput").is_valid(case["input"]) != case["valid"]:
            raise AssertionError("identity: " + str(index))
        if case["valid"] and normalize_trace(case["input"]) != case["canonical"]:
            raise AssertionError("identity normalization: " + str(index))
    counts["identity"] = len(cases)
    table = load("scope-fixtures.json")
    for case in table["cases"]:
        same = scoped_key(table["base"], table["key_fields"]) == scoped_key({**table["base"], **case["overrides"]}, table["key_fields"])
        actual = "distinct" if not same else "replay" if case["request_equal"] else "conflict"
        if actual != case["expected"]:
            raise AssertionError("scope: " + case["id"])
    counts["scope"] = len(table["cases"])
    table = load("lifecycle-fixtures.json")
    for case in table["cases"]:
        if lifecycle_result(table, case["events"]) != (case["readable_after_each"], case["final"]):
            raise AssertionError("lifecycle: " + case["id"])
    counts["lifecycle"] = len(table["cases"])
    table = load("recording-import-fixtures.json")
    for case in table["cases"]:
        scenario = fixture_override(table["base"], case["overrides"])
        check = {"admission": validate_import_admission, "completion": validate_import_completion}[case["stage"]]
        try:
            check(scenario)
            valid = True
        except (ValueError, jsonschema_error):
            valid = False
        if valid != case["valid"]:
            raise AssertionError("recording import: " + case["id"])
    counts["recording_import"] = len(table["cases"])
    return counts


if __name__ == "__main__":
    try:
        print(json.dumps({"status": "PASS", "cases": validate_all()}, sort_keys=True))
    except (AssertionError, ValueError, jsonschema_error) as error:
        # Wire validation errors can include payloads. Print only type, not contents.
        print(json.dumps({"status": "FAIL", "error_type": type(error).__name__}))
        sys.exit(1)
