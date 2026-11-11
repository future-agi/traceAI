"""Explicit session-owned file leases. Arbitrary LocalFile remains disabled.

Bind a trusted session root during application setup/finalization, not an audio
callback. Report paths are never authority. All traversal uses no-follow directory
FDs, regular files only, and an expected identity supplied by the owning session.
"""

import os
import stat
import threading
from dataclasses import dataclass, field

from .protocol import TransferError


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
        if not os.path.isabs(root) or not hasattr(os, "O_NOFOLLOW"):
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

    def source(self, relative_path: str, *, device: int, inode: int, size: int) -> "OwnedRecordingFile":
        # No file operations here. The adapter must obtain identity from the
        # actual owned file, not trust an arbitrary report/tool-supplied path.
        if not isinstance(relative_path, str) or len(relative_path) > 4096:
            raise ValueError("invalid owned recording path")
        parts = relative_path.split("/")
        if not parts or any(part in ("", ".", "..") for part in parts) or len(parts) > 32:
            raise ValueError("invalid owned recording path")
        if any(type(v) is not int or v < 0 for v in (device, inode, size)):
            raise ValueError("invalid owned recording identity")
        return OwnedRecordingFile(self, tuple(parts), device, inode, size)

    def _open(self, source: "OwnedRecordingFile") -> "FileLease":
        if os.getpid() != self._pid:
            raise TransferError("source_unavailable")
        with self._lock:
            if self._fd < 0:
                raise TransferError("source_unavailable")
            fd = os.dup(self._fd)
        try:
            for part in source._parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            file_fd = os.open(source._parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                info = os.fstat(file_fd)
                if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino, info.st_size) != (source.device, source.inode, source.size):
                    raise TransferError("source_unavailable")
                return FileLease(file_fd, info)
            except Exception:
                os.close(file_fd)
                raise
        except Exception:
            raise TransferError("source_unavailable") from None
        finally:
            os.close(fd)

    def close(self) -> None:
        if os.getpid() != self._pid:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1
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
    def __init__(self, fd: int, info):
        self._fd, self._info = fd, info

    def __len__(self):
        return self._info.st_size

    def read(self, offset: int, size: int) -> bytes:
        self.check()
        return os.pread(self._fd, size, offset)

    def check(self) -> None:
        info = os.fstat(self._fd)
        original = self._info
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
            original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns, original.st_ctime_ns
        ):
            raise TransferError("source_unavailable")

    def close(self) -> None:
        os.close(self._fd)
