"""Bounded, local-only artifact IO. Never follow artifact symlinks."""

from __future__ import annotations

import json
import math
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MAX_BYTES = 8 * 1024 * 1024
MAX_NODES = 50000


class ArtifactError(ValueError):
    """An artifact was unsafe, incomplete or invalid (no artifact text attached)."""


def component(value: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\0" in value
    ):
        raise ArtifactError("unsafe_path")
    return value


@contextmanager
def directory(path: Path) -> Iterator[int]:
    # Walk from the filesystem root with openat: no resolve-then-open race.
    parts = path.absolute().parts
    fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[1:]:
            next_fd = os.open(
                component(part), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def read_bytes(root_fd: int, parts: tuple[str, ...], limit: int = MAX_BYTES) -> bytes:
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = os.open(
                component(part), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = next_fd
        file_fd = os.open(
            component(parts[-1]), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
        )
        with os.fdopen(file_fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > limit
            ):
                raise ArtifactError("artifact_size_or_type")
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise ArtifactError("artifact_size")
            return data
    finally:
        os.close(fd)


def parse(data: bytes) -> Any:
    def invalid_constant(_):
        raise ArtifactError("nonfinite_number")

    try:
        value = json.loads(data, parse_constant=invalid_constant)
        stack = [(value, 0)]
        count = 0
        while stack:
            item, depth = stack.pop()
            count += 1
            if count > MAX_NODES or depth > 24:
                raise ArtifactError("artifact_complexity")
            if isinstance(item, float) and not math.isfinite(item):
                raise ArtifactError("nonfinite_number")
            if isinstance(item, dict):
                stack.extend((v, depth + 1) for v in item.values())
            elif isinstance(item, list):
                stack.extend((v, depth + 1) for v in item)
        return value
    except (ValueError, RecursionError, UnicodeError):
        raise ArtifactError("invalid_json") from None


def read_json(root_fd: int, *parts: str) -> Any:
    return parse(read_bytes(root_fd, parts))
