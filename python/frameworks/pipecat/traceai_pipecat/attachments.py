"""Fail-closed Pipecat recording binding until the shared stream API is ready.

Nonzero AudioBufferProcessor.buffer_size is a flush threshold, not a hard memory
cap: oversized frames, wall-clock silence and turn buffers can exceed it. Do not
label a series of unmanifested clips as a complete conversation recording.
"""

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

from fi_instrumentation.attachments import CapturePolicy, CorrelationHandle
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY, get_value

_SUPPORTED = frozenset(("1.0.0", "1.15.0"))


@dataclass(frozen=True)
class AudioSessionContext:
    """The actual conversation identity, never a late ambient span lookup."""

    correlation: CorrelationHandle

    @classmethod
    def from_span(cls, span):
        return cls(CorrelationHandle.from_span(span))


@dataclass(frozen=True)
class AudioCaptureBinding:
    """A status is not evidence of a recorder or a recording source."""

    status: str
    processor: object = None


def _admission(manager, session_context, *, new_recording):
    try:
        capture = CapturePolicy(new_recording=new_recording)
        session = manager._session
        reason = manager.policy.evaluate(
            session.policy if session else None, capture, direction="mixed",
            mime_type="audio/wav", source_kind="Bytes",
        )
        if reason:
            return reason
        if get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return "policy_denied"
        if manager.status != "ready":
            return manager.status
        if not isinstance(session_context, AudioSessionContext):
            return "correlation_unavailable"
        if not session_context.correlation.sampled:
            return "unsampled"
    except Exception:
        return "source_unavailable"
    return None


def bind_audio_processor(processor, session_context, *, manager):
    """Inspect eligibility for an existing recorder; never start or mutate it.

    This component currently returns ``streaming_unavailable`` for otherwise
    eligible processors. P02 still lacks stream/manifest transport. In
    particular, it must not register a callback which retains PCM, uploads
    unlinked chunks, or pretends the upstream flush threshold is a hard bound.
    Both auto and explicit callers receive this same truthful state.
    """
    reason = _admission(manager, session_context, new_recording=False)
    if reason:
        return AudioCaptureBinding(reason)
    try:
        if version("pipecat-ai") not in _SUPPORTED:
            return AudioCaptureBinding("unsupported_version")
        from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
        if not isinstance(processor, AudioBufferProcessor):
            return AudioCaptureBinding("unsupported_version")
        if (type(processor._buffer_size) is not int or not 0 < processor._buffer_size <= 128 * 1024
                or processor._enable_turn_audio):
            return AudioCaptureBinding("unbounded_source")
    except PackageNotFoundError:
        return AudioCaptureBinding("unsupported_version")
    except Exception:
        return AudioCaptureBinding("source_unavailable")
    return AudioCaptureBinding("streaming_unavailable")


def create_audio_capture(session_context, *, manager):
    """Do not create/start a new recorder without both recording permissions.

    Even with permission, this release fails closed until bounded upstream
    processing, stream segments, gaps and manifests can be transported. No
    processor is returned for pipeline insertion; existing audio is untouched.
    """
    reason = _admission(manager, session_context, new_recording=True)
    return AudioCaptureBinding(reason or "streaming_unavailable")
