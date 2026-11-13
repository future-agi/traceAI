"""Explicit session-owned file leases (design section 4). Arbitrary LocalFile stays disabled.

Bind a trusted session root during application setup/finalization, never on an
audio callback. Report paths are never authority. All traversal uses no-follow
directory FDs, regular files only, and an identity taken from the opened file.
Windows is unsupported (``source_unavailable``).
"""

import os
import stat
import struct
import threading
from dataclasses import dataclass, field
from typing import Optional

from .errors import TransferError


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int


def _parts(relative_path) -> tuple:
    if not isinstance(relative_path, str) or len(relative_path) > 4096:
        raise ValueError("invalid owned recording path")
    parts = relative_path.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts) or len(parts) > 32:
        raise ValueError("invalid owned recording path")
    return tuple(parts)


class RecordingFileScope:
    """An explicit read permission for files owned by one bound SDK session.

    The binding adapter owns closing this scope, after its transfer/finalization
    deadline. Creating this permission does not start a recorder.
    """

    def __init__(self, root: str, *, enabled: bool = False):
        self._fd = -1
        self._pid = os.getpid()
        self._lock = threading.Lock()
        if not enabled:
            raise ValueError("owned recording files require explicit permission")
        if os.name != "posix" or not os.path.isabs(root) or not hasattr(os, "O_NOFOLLOW"):
            raise ValueError("unsupported recording root")
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in root.split("/"):
                if not part:
                    continue
                if part in (".", ".."):
                    raise ValueError("invalid recording root")
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            self._fd = fd
        except Exception:
            os.close(fd)
            raise ValueError("recording root unavailable") from None

    def __enter__(self) -> "RecordingFileScope":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _open_parts(self, parts: tuple) -> int:
        if os.getpid() != self._pid:
            self._forget_after_fork()
            raise TransferError("source_unavailable")
        with self._lock:
            if self._fd < 0:
                raise TransferError("source_unavailable")
            fd = os.dup(self._fd)
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except Exception:
            raise TransferError("source_unavailable") from None
        finally:
            os.close(fd)
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise TransferError("source_unavailable")
        except Exception:
            os.close(file_fd)
            raise TransferError("source_unavailable") from None
        return file_fd

    def identify(self, relative_path: str) -> FileIdentity:
        """Secure open + fstat + close; never ``os.stat`` on a path."""
        file_fd = self._open_parts(_parts(relative_path))
        try:
            info = os.fstat(file_fd)
            return FileIdentity(info.st_dev, info.st_ino, info.st_size)
        finally:
            os.close(file_fd)

    def source(self, relative_path: str, identity: Optional[FileIdentity] = None, *, device: Optional[int] = None,
               inode: Optional[int] = None, size: Optional[int] = None) -> "OwnedRecordingFile":
        # No file operations here. Identity must come from ``identify`` (or the
        # owning session), not from an arbitrary report/tool-supplied path.
        if identity is None:
            identity = FileIdentity(device, inode, size)
        if any(type(v) is not int or v < 0 for v in (identity.device, identity.inode, identity.size)):
            raise ValueError("invalid owned recording identity")
        return OwnedRecordingFile(self, _parts(relative_path), identity.device, identity.inode, identity.size)

    def _open(self, source: "OwnedRecordingFile") -> "FileLease":
        file_fd = self._open_parts(source._parts)
        try:
            info = os.fstat(file_fd)
            if (info.st_dev, info.st_ino, info.st_size) != (source.device, source.inode, source.size):
                raise TransferError("source_unavailable")
            return FileLease(file_fd, info)
        except Exception:
            os.close(file_fd)
            raise TransferError("source_unavailable") from None

    def _forget_after_fork(self) -> None:
        # A child must not use an inherited lock; just close the duplicated fd.
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1

    def close(self) -> None:
        if os.getpid() != self._pid:
            self._forget_after_fork()
            return
        with self._lock:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1


@dataclass(frozen=True)
class OwnedRecordingFile:
    _scope: RecordingFileScope = field(repr=False)
    _parts: tuple = field(repr=False)
    device: int = field(repr=False)
    inode: int = field(repr=False)
    size: int = field(repr=False)

    def _open(self) -> "FileLease":
        return self._scope._open(self)


class FileLease:
    """Internal: obtained by the worker, never by adapters. ``pread`` only."""

    def __init__(self, fd: int, info):
        self._fd, self._info = fd, info

    def __len__(self):
        return self._info.st_size

    def read(self, offset: int, size: int) -> bytes:
        self.check()
        return os.pread(self._fd, size, offset)

    def check(self) -> None:
        if self._fd < 0:
            raise TransferError("source_unavailable")
        info = os.fstat(self._fd)
        original = self._info
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
            original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns, original.st_ctime_ns
        ):
            raise TransferError("source_unavailable")

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


class MemorySource:
    """``ContentSource`` over bytes or a pinned memoryview."""

    def __init__(self, data):
        self._data = memoryview(data).cast("B") if not isinstance(data, bytes) else data

    def __len__(self) -> int:
        return len(self._data)

    def read(self, offset: int, size: int):
        return self._data[offset:offset + size]

    def check(self) -> None:
        return None


def wav_header(channels: int, rate: int, frames: int) -> bytes:
    """Canonical 44-byte RIFF/WAVE PCM s16le header."""
    data = frames * channels * 2
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + data, b"WAVE", b"fmt ", 16, 1, channels, rate,
                       rate * channels * 2, channels * 2, 16, b"data", data)


@dataclass(frozen=True)
class WavInfo:
    channels: int
    rate: int
    data_offset: int
    data_bytes: int

    @property
    def frames(self) -> int:
        return self.data_bytes // (self.channels * 2)


def parse_wav(header: bytes, file_size: int) -> Optional[WavInfo]:
    """Strict RIFF/WAVE PCM 16-bit parse of at most 4 KiB; None when unsupported."""
    try:
        if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            return None
        offset, fmt = 12, None
        while offset + 8 <= len(header):
            chunk, size = struct.unpack_from("<4sI", header, offset)
            body = offset + 8
            if chunk == b"fmt ":
                if size < 16 or body + size > len(header):
                    return None
                tag, channels, rate, _, align, bits = struct.unpack_from("<HHIIHH", header, body)
                if tag == 0xFFFE:
                    if size < 40 or header[body + 24:body + 26] != b"\x01\x00":
                        return None
                elif tag != 1:
                    return None
                if bits != 16 or not 1 <= channels <= 32 or not 8000 <= rate <= 192000 or align != channels * 2:
                    return None
                fmt = (channels, rate)
            elif chunk == b"data":
                if fmt is None or body + size != file_size or size % (fmt[0] * 2):
                    return None
                return WavInfo(fmt[0], fmt[1], body, size)
            offset = body + size + (size & 1)
        return None
    except struct.error:
        return None


class WavSegmentSource:
    """A synthesized 44-byte header followed by ``pread`` of a frame range."""

    def __init__(self, lease: FileLease, info: WavInfo, start_frame: int, frames: int):
        self._lease, self._header = lease, wav_header(info.channels, info.rate, frames)
        self._start = info.data_offset + start_frame * info.channels * 2
        self._length = 44 + frames * info.channels * 2

    def __len__(self) -> int:
        return self._length

    def read(self, offset: int, size: int) -> bytes:
        result = b""
        if offset < 44:
            result = self._header[offset:min(44, offset + size)]
            size -= len(result)
            offset = 44
        if size > 0:
            result += self._lease.read(self._start + offset - 44, size)
        return result

    def check(self) -> None:
        self._lease.check()
