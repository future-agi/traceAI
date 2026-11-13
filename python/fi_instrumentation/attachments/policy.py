"""Attachment admission is independent of span masking and always deny-wins.

The decision is the F01 privacy table (``_privacy_table.json``, vendored from
``spec/attachments/v1/privacy-table.json``) evaluated over facts built here.
This module never inspects a source. Text PII redaction does not claim to
sanitize media, and server-side validation is not pre-egress redaction.
"""

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

from fi_instrumentation.instrumentation.config import TraceConfig

from .types import MIME_MODALITIES, PURPOSES

MiB = 1024 * 1024


def _flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("true", "1", "yes")


def _restriction(name: str) -> bool:
    # Invalid hide/PII configuration is a restriction, never permission.
    value = os.getenv(name)
    return value is not None and value.strip().lower() not in ("false", "0", "no")


def env_disabled() -> bool:
    """An explicit environment disable cannot be overridden by code or config."""
    return os.getenv("FI_ATTACHMENTS_ENABLED") is not None and not _flag("FI_ATTACHMENTS_ENABLED")


# name: (default, ceiling, integer?)
_BOUNDS = {
    "max_descriptors": (128, 128, True),
    "max_owned_bytes": (32 * MiB, 32 * MiB, True),
    "workers": (2, 2, True),
    "chunk_bytes": (256 * 1024, 256 * 1024, True),
    "sync_copy_bytes": (8 * MiB, 32 * MiB, True),
    "window_segments": (2, 4, True),
    "request_timeout": (2.0, 30.0, False),
    "content_timeout": (120.0, 600.0, False),
    "manifest_timeout": (10.0, 30.0, False),
    "shutdown_timeout": (5.0, 30.0, False),
    "finalization_timeout": (30.0, 30.0, False),
    "ticket_ttl": (10.0, 30.0, False),
    "stream_idle_ttl": (60.0, 300.0, False),
}


@dataclass(frozen=True)
class AttachmentConfig:
    """Client configuration. Every flag defaults OFF.

    ``enabled`` and ``record_audio``/``record_video`` fall back to the
    ``FI_ATTACHMENTS_*`` environment once, when this object is built.
    """

    enabled: Optional[bool] = None
    record_audio: Optional[bool] = None
    record_video: Optional[bool] = None
    allow_unredacted_media_with_text_pii: Optional[bool] = None
    endpoint: Optional[str] = field(default=None, repr=False)
    workspace_id: Optional[str] = None
    max_descriptors: int = 128
    max_owned_bytes: int = 32 * MiB
    workers: int = 2
    chunk_bytes: int = 256 * 1024
    sync_copy_bytes: int = 8 * MiB
    window_segments: int = 2
    request_timeout: float = 2.0
    content_timeout: float = 120.0
    manifest_timeout: float = 10.0
    shutdown_timeout: float = 5.0
    finalization_timeout: float = 30.0
    ticket_ttl: float = 10.0
    stream_idle_ttl: float = 60.0

    def __post_init__(self) -> None:
        for name in ("enabled", "record_audio", "record_video", "allow_unredacted_media_with_text_pii"):
            value = getattr(self, name)
            if value is None:
                object.__setattr__(self, name, _flag("FI_ATTACHMENTS_" + name.upper()))
            elif type(value) is not bool:
                raise ValueError("attachment flags must be booleans")
        if self.endpoint is None:
            object.__setattr__(self, "endpoint", os.getenv("FI_ATTACHMENTS_ENDPOINT"))
        if self.workspace_id is not None and not isinstance(self.workspace_id, str):
            raise ValueError("invalid attachment workspace")
        for name, (_, ceiling, integer) in _BOUNDS.items():
            value = getattr(self, name)
            if integer:
                if type(value) is not int or not 0 < value <= ceiling:
                    raise ValueError("invalid attachment resource bound")
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= ceiling:
                raise ValueError("invalid attachment deadline")


@dataclass(frozen=True)
class ServerPolicy:
    """Negotiated F01 ``Policy``; an unknown policy is never permissive."""

    enabled: bool = False
    credential_write: bool = False
    allowed_sources: frozenset = frozenset()
    allowed_modalities: frozenset = frozenset(("audio",))
    allowed_directions: frozenset = frozenset(("input", "output", "mixed"))
    require_media_redaction: bool = True
    require_metadata_removal: bool = True
    allow_unredacted_media_with_text_pii: bool = False
    record_audio: bool = False
    record_video: bool = False
    version: str = ""
    deletion_generation: int = 0

    def tighter_than(self, other: "ServerPolicy") -> bool:
        """True when any admission allowed by ``self`` might be denied by ``other``."""
        return (self.version != other.version or (self.enabled and not other.enabled)
                or (self.credential_write and not other.credential_write)
                or not other.allowed_sources >= self.allowed_sources
                or not other.allowed_modalities >= self.allowed_modalities
                or not other.allowed_directions >= self.allowed_directions
                or (other.require_media_redaction and not self.require_media_redaction)
                or (other.require_metadata_removal and not self.require_metadata_removal)
                or (self.allow_unredacted_media_with_text_pii and not other.allow_unredacted_media_with_text_pii)
                or (self.record_audio and not other.record_audio)
                or (self.record_video and not other.record_video))


@dataclass(frozen=True)
class CapturePolicy:
    """Adapter provenance, intersected with every other policy layer.

    Adapters must preserve model-input ancestry even for tool outputs. Unknown
    direction is treated as mixed; a caller cannot relabel a mixed recording.
    No qualified local media sanitizer is installed by this runtime.
    """

    capture_content: bool = True
    record_inputs: bool = True
    record_outputs: bool = True
    muted: bool = False
    temporary_chat: bool = False
    requested: bool = True
    model_input: bool = False
    contains_input: bool = False
    contains_output: bool = False
    error_detail: bool = False
    # Deprecated alias for operation="create_recording"; kept deny-wins.
    new_recording: bool = False

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in vars(self).values()):
            raise ValueError("capture policy facts must be booleans")


@lru_cache(maxsize=1)
def _table():
    from jsonschema import Draft202012Validator  # enabled managers only

    table = json.loads(Path(__file__).with_name("_privacy_table.json").read_text())
    context = Draft202012Validator(table["context_schema"])
    rules = tuple((rule["reason"], Draft202012Validator(rule["deny_if"])) for rule in table["rules"])
    return context, rules


def decide(facts: dict) -> Optional[str]:
    """Interpret the ordered F01 privacy table. None means allowed (``queued``)."""
    context, rules = _table()
    if not context.is_valid(facts):
        return "policy_denied"
    for reason, rule in rules:
        if rule.is_valid(facts):
            return reason
    return None


_HIDE = ("hide_inputs", "hide_outputs", "hide_input_messages", "hide_output_messages",
         "hide_input_images", "hide_input_text", "hide_output_text", "hide_error_details", "pii_redaction")


class AttachmentPolicy:
    def __init__(self, config: AttachmentConfig, trace_config: Optional[TraceConfig] = None):
        self.config = config
        self.trace_config = trace_config or TraceConfig()

    def _hidden(self, name: str) -> bool:
        return bool(getattr(self.trace_config, name, False)) or _restriction("FI_" + name.upper())

    def facts(self, server: Optional[ServerPolicy], capture: CapturePolicy, *, direction: str,
              mime_type: str, source_kind: str, operation: str = "observe_consumed",
              purpose: Optional[str] = None, import_source: bool = False) -> dict:
        """Build the F01 privacy-table context. Unknown facts fail closed."""
        modality = MIME_MODALITIES.get(mime_type, "file")
        known = server is not None
        server = server or ServerPolicy()
        ancestry = purpose in ("model_input", "computer_screenshot") or capture.model_input
        record_audio = self.config.record_audio and (
            os.getenv("FI_ATTACHMENTS_RECORD_AUDIO") is None or _flag("FI_ATTACHMENTS_RECORD_AUDIO"))
        record_video = self.config.record_video and (
            os.getenv("FI_ATTACHMENTS_RECORD_VIDEO") is None or _flag("FI_ATTACHMENTS_RECORD_VIDEO"))
        if capture.new_recording:
            operation = "create_recording"
        facts = {
            "enabled": self.config.enabled and not env_disabled(),
            "endpoint_configured": True,
            "policy_known": known,
            "server_allowed": server.enabled,
            "credential_allowed": server.credential_write,
            "integration_capture_content": capture.capture_content,
            "integration_record_inputs": capture.record_inputs,
            "integration_record_outputs": capture.record_outputs,
            "muted": capture.muted,
            "temporary_chat": capture.temporary_chat,
            "server_require_media_redaction": server.require_media_redaction,
            "qualified_client_sanitizer": False,
            "allow_unredacted_media_with_text_pii": self.config.allow_unredacted_media_with_text_pii,
            "server_allow_unredacted_media_with_text_pii": server.allow_unredacted_media_with_text_pii,
            "require_metadata_removal": server.require_metadata_removal,
            "qualified_client_metadata_removal": False,
            "modality_allowed": modality in server.allowed_modalities and direction in server.allowed_directions,
            "source_authorized": source_kind in server.allowed_sources,
            "call_requested": capture.requested,
            # Steps 9-12 of preflight check these against the real target/budget.
            "sampled": True, "correlation_available": True, "generation_current": True,
            "source_available": True, "budget_available": True, "queue_available": True,
            "direction": direction,
            "semantic_input": direction in ("input", "mixed") or capture.contains_input or ancestry,
            "semantic_output": direction in ("output", "mixed") or capture.contains_output,
            "model_bound_image": modality == "image" and ancestry,
            "opaque_text_possible": True,
            "error_diagnostic": capture.error_detail or purpose == "diagnostic",
            "modality": modality,
            "source_kind": source_kind,
            "operation": operation,
            "record_audio": record_audio,
            "record_video": record_video,
            "server_record_audio": server.record_audio,
            "server_record_video": server.record_video,
            "browser_remote_fetch": False,
            # The SDK never fetches. Only a server-minted envelope import (whose
            # fetch the trusted importer qualified) reaches a RemoteUrl kind.
            "remote_destination_approved": import_source, "all_addresses_public": import_source,
            "tls_ip_pinned": import_source, "redirects_revalidated": import_source,
            "ambient_credentials_absent": import_source,
            # Only scope-created OwnedRecordingFile objects reach this point.
            "owned_session_bound": source_kind == "OwnedRecordingFile",
            "owned_file_lease_valid": source_kind == "OwnedRecordingFile",
            # Provider connection authority and completion are checked by the
            # server and the resolver respectively; only the import path sets them.
            "provider_connection_authorized": import_source,
            "provider_output_completed": import_source,
            "explicit_partial_allowed": False,
        }
        for name in _HIDE:
            facts[name] = self._hidden(name)
        return facts

    def evaluate(self, server: Optional[ServerPolicy], capture: CapturePolicy, *,
                 direction: str, mime_type: str, source_kind: str, operation: str = "observe_consumed",
                 purpose: Optional[str] = None, import_source: bool = False) -> Optional[str]:
        """Deny-wins privacy decision; returns the first denial reason or None."""
        if not self.config.enabled or env_disabled():
            return "disabled"
        if server is None:
            return "bootstrap_pending"
        if direction not in ("input", "output", "mixed") or (purpose is not None and purpose not in PURPOSES):
            return "policy_denied"
        if mime_type not in MIME_MODALITIES:
            return "source_unavailable"
        return decide(self.facts(server, capture, direction=direction, mime_type=mime_type,
                                 source_kind=source_kind, operation=operation, purpose=purpose,
                                 import_source=import_source))
