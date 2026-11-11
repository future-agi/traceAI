"""Opt-in, provider-owned audio attachments.

Capture OFF and recording OFF by default. Importing an existing owned recording
never starts a recorder. RemoteUrl/ProviderFile/LocalFile are typed but fail closed
until their separately qualified retrieval adapters exist.
"""

from .manager import AttachmentManager, AttachmentSpanProcessor
from .policy import AttachmentConfig, AttachmentPolicy, CapturePolicy, ServerPolicy
from .sources import OwnedRecordingFile, RecordingFileScope
from .types import (
    AttachmentHandle, Bytes, Completion, CorrelationHandle, FlushResult,
    LocalFile, ProviderFile, RemoteUrl,
)

__all__ = [
    "AttachmentConfig", "AttachmentManager", "AttachmentPolicy", "AttachmentSpanProcessor",
    "AttachmentHandle", "Bytes", "CapturePolicy", "Completion", "CorrelationHandle", "FlushResult",
    "LocalFile", "OwnedRecordingFile", "ProviderFile", "RecordingFileScope", "RemoteUrl", "ServerPolicy",
]
