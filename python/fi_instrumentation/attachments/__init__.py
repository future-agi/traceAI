"""Opt-in, provider-owned attachments (audio, images, documents, files).

Capture is OFF by default and creating new recordings is separately OFF.
``create_attachment_manager`` returns a strict no-op null object when OFF: it
starts no thread or timer, registers no fork hook and imports neither the wire
adapter (``protocol``) nor ``jsonschema``. Importing an existing owned
recording never starts a recorder. RemoteUrl/ProviderFile/LocalFile are typed
but fail closed outside the completed-recording import seam.
"""

from ._disabled import DisabledAttachmentManager, OwnedRecordingHandle
from .admission import AdmissionTicket
from .errors import TransferError
from .imports import (
    CompletedRecordingRequest, CompletedRecordingResolver, EgressOutputRegistry, EnvelopeSource, ImportHandle,
    ProviderFileSource, ResolvedCompletedRecording, UnavailableResolver,
)
from .manager import AttachmentManager, AttachmentSpanProcessor, create_attachment_manager
from .policy import AttachmentConfig, AttachmentPolicy, CapturePolicy, ServerPolicy
from .recording import PcmFormat, RecordingOptions, RecordingResult, RecordingStatus, RecordingWriter, TrackSpec
from .sources import FileIdentity, OwnedRecordingFile, RecordingFileScope
from .stream import AttachmentStream, StreamOptions
from .types import (
    AttachmentHandle, AttachmentStatus, Bytes, Completion, CorrelationHandle, FlushResult,
    LocalFile, ProviderFile, RemoteUrl,
)

__all__ = [
    "AdmissionTicket", "AttachmentConfig", "AttachmentHandle", "AttachmentManager", "AttachmentPolicy",
    "AttachmentSpanProcessor", "AttachmentStatus", "AttachmentStream", "Bytes", "CapturePolicy", "Completion",
    "CompletedRecordingRequest", "CompletedRecordingResolver", "CorrelationHandle", "DisabledAttachmentManager",
    "EgressOutputRegistry", "EnvelopeSource", "FileIdentity", "FlushResult", "ImportHandle", "LocalFile",
    "OwnedRecordingFile", "OwnedRecordingHandle", "PcmFormat", "ProviderFile", "ProviderFileSource",
    "RecordingFileScope", "RecordingOptions", "RecordingResult", "RecordingStatus", "RecordingWriter", "RemoteUrl",
    "ResolvedCompletedRecording", "ServerPolicy", "StreamOptions", "TrackSpec", "TransferError",
    "UnavailableResolver", "create_attachment_manager",
]
