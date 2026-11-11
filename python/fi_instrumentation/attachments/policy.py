"""Attachment admission is independent of span masking and always deny-wins.

This module never inspects a source. In particular, text PII redaction does not
claim to sanitize audio. Server-side validation is not pre-egress redaction.
"""

import os
from dataclasses import dataclass, field
from typing import Optional

from fi_instrumentation.instrumentation.config import TraceConfig


def _flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("true", "1", "yes")


def _restriction(name: str) -> bool:
    # Invalid hide/PII configuration is a restriction, never permission.
    value = os.getenv(name)
    return value is not None and value.strip().lower() not in ("false", "0", "no")


@dataclass(frozen=True)
class AttachmentConfig:
    enabled: Optional[bool] = None
    record_audio: Optional[bool] = None
    record_video: Optional[bool] = None
    allow_unredacted_media_with_text_pii: Optional[bool] = None
    endpoint: Optional[str] = field(default=None, repr=False)
    max_descriptors: int = 128
    max_owned_bytes: int = 32 * 1024 * 1024
    max_attachment_bytes: int = 8 * 1024 * 1024
    workers: int = 2
    chunk_bytes: int = 256 * 1024
    request_timeout: float = 2.0
    shutdown_timeout: float = 5.0

    def __post_init__(self) -> None:
        for name in ("enabled", "record_audio", "record_video", "allow_unredacted_media_with_text_pii"):
            value = getattr(self, name)
            if value is None:
                object.__setattr__(self, name, _flag("FI_ATTACHMENTS_" + name.upper()))
            elif type(value) is not bool:
                raise ValueError("attachment flags must be booleans")
        if self.endpoint is None:
            object.__setattr__(self, "endpoint", os.getenv("FI_ATTACHMENTS_ENDPOINT"))
        for name, ceiling in (("max_descriptors", 128), ("max_owned_bytes", 32 * 1024 * 1024),
                              ("max_attachment_bytes", 8 * 1024 * 1024), ("workers", 2),
                              ("chunk_bytes", 256 * 1024)):
            value = getattr(self, name)
            if type(value) is not int or not 0 < value <= ceiling:
                raise ValueError("invalid attachment resource bound")
        for value in (self.request_timeout, self.shutdown_timeout):
            if not isinstance(value, (int, float)) or not 0 < value <= 30:
                raise ValueError("invalid attachment deadline")


@dataclass(frozen=True)
class ServerPolicy:
    """Negotiated policy; unknown policy is never permissive."""

    enabled: bool = False
    credential_write: bool = False
    allowed_sources: frozenset[str] = frozenset()
    allowed_directions: frozenset[str] = frozenset()
    require_media_redaction: bool = True
    require_metadata_removal: bool = True
    allow_unredacted_media_with_text_pii: bool = False
    record_audio: bool = False
    version: str = ""
    deletion_generation: int = 0


@dataclass(frozen=True)
class CapturePolicy:
    """Adapter provenance, intersected with every other policy layer.

    Adapters must preserve model-input ancestry even for tool outputs. Unknown
    direction is treated as mixed; a caller cannot relabel a mixed recording.
    No qualified local audio sanitizer is installed by this runtime.
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
    new_recording: bool = False


    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in vars(self).values()):
            raise ValueError("capture policy facts must be booleans")


class AttachmentPolicy:
    def __init__(self, config: AttachmentConfig, trace_config: Optional[TraceConfig] = None):
        self.config = config
        self.trace_config = trace_config or TraceConfig()

    def evaluate(self, server: Optional[ServerPolicy], capture: CapturePolicy, *,
                 direction: str, mime_type: str, source_kind: str) -> Optional[str]:
        if not self.config.enabled:
            return "disabled"
        # An explicit environment disable cannot be overridden by a call/config.
        if os.getenv("FI_ATTACHMENTS_ENABLED") is not None and not _flag("FI_ATTACHMENTS_ENABLED"):
            return "disabled"
        if server is None:
            return "bootstrap_pending"
        if not server.enabled:
            return "policy_denied"
        if not server.credential_write:
            return "credential_scope_unsupported"
        if not capture.capture_content or capture.muted or capture.temporary_chat or not capture.requested:
            return "policy_denied"
        inputs = direction in ("input", "mixed") or capture.contains_input or capture.model_input
        outputs = direction in ("output", "mixed") or capture.contains_output
        if direction not in ("input", "output", "mixed"):
            return "policy_denied"
        if inputs and not capture.record_inputs or outputs and not capture.record_outputs:
            return "policy_denied"

        def hidden(name: str) -> bool:
            return bool(getattr(self.trace_config, name)) or _restriction("FI_" + name.upper())

        if inputs and any(hidden(n) for n in ("hide_inputs", "hide_input_messages", "hide_input_text")):
            return "policy_denied"
        if outputs and any(hidden(n) for n in ("hide_outputs", "hide_output_messages", "hide_output_text")):
            return "policy_denied"
        if inputs and mime_type.startswith("image/") and hidden("hide_input_images"):
            return "policy_denied"
        if capture.error_detail and hidden("hide_error_details"):
            return "policy_denied"
        if server.require_media_redaction or server.require_metadata_removal:
            return "redaction_unavailable"
        if hidden("pii_redaction") and not (
            self.config.allow_unredacted_media_with_text_pii
            and server.allow_unredacted_media_with_text_pii
        ):
            return "redaction_unavailable"
        if direction not in server.allowed_directions or source_kind not in server.allowed_sources:
            return "policy_denied"
        record_audio = self.config.record_audio and server.record_audio
        if os.getenv("FI_ATTACHMENTS_RECORD_AUDIO") is not None:
            record_audio = record_audio and _flag("FI_ATTACHMENTS_RECORD_AUDIO")
        if capture.new_recording and not record_audio:
            return "recording_disabled"
        # Wave A is audio only. Other modalities remain explicitly unsupported.
        if mime_type not in ("audio/wav", "audio/L16", "audio/flac", "audio/mpeg", "audio/ogg", "audio/mp4", "audio/webm"):
            return "source_unavailable"
        return None
