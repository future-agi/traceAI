"""Shared finalization/backfill path with local acknowledgement bookkeeping."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from harbor.models.job.result import JobResult

from ._artifacts import ArtifactError, directory, read_json
from ._convert import convert_trial
from ._transport import DeliveryError, Settings, send


@dataclass
class ImportSummary:
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    conflicts: int = 0
    incomplete: int = 0
    errors: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.conflicts == 0


@contextmanager
def _manifest(root_fd, destination):
    # Per-trial acknowledgements avoid rewriting an O(job) manifest on every hook.
    state = os.dup(root_fd)
    lock = None
    try:
        for name in (".traceai-harbor", destination):
            try:
                os.mkdir(name, mode=0o700, dir_fd=state)
            except FileExistsError:
                pass
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=state
            )
            os.close(state)
            state = child
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        # On macOS, concurrent O_CREAT|O_NOFOLLOW can spuriously return ENOENT.
        # Create exclusively, or open the existing inode without O_CREAT. Never
        # unlink a lock file: another importer may still hold its inode's lock.
        try:
            lock = os.open("lock", flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=state)
        except FileExistsError:
            lock = os.open("lock", flags, dir_fd=state)
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ArtifactError("unsafe_lock")
        deadline = time.monotonic() + 35
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ArtifactError("import_busy") from None
                time.sleep(0.05)
        yield state
    finally:
        if lock is not None:
            os.close(lock)
        os.close(state)


def _acknowledged(state: int, trial_id: str) -> str | None:
    try:
        manifest = read_json(state, trial_id + ".json")
    except FileNotFoundError:
        return None
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ArtifactError("invalid_manifest")
    digest = manifest.get("digest")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ArtifactError("invalid_manifest")
    return digest


def _acknowledge(state: int, trial_id: str, digest: str) -> None:
    temporary = ".pending-" + uuid4().hex
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=state,
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps({"version": 1, "digest": digest}).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, trial_id + ".json", src_dir_fd=state, dst_dir_fd=state)
        os.fsync(state)
    finally:
        try:
            os.unlink(temporary, dir_fd=state)
        except FileNotFoundError:
            pass


def import_job(
    job_dir: str | Path, *, settings: Settings | None = None
) -> ImportSummary:
    """Import a finished local job. Failures never acknowledge or overwrite old traces.

    Keep .traceai-harbor with the job. A crash after remote acceptance but before
    the local fsync can retransmit; OTLP does not guarantee receiver deduplication.
    """
    return _import(job_dir, settings or Settings.from_env())


def _import(
    job_dir: str | Path,
    settings: Settings,
    trial_name: str | None = None,
    *,
    live_job_id: UUID | None = None,
) -> ImportSummary:
    summary = ImportSummary()
    errors = Counter()
    try:
        with directory(Path(job_dir)) as root:
            if trial_name is not None and live_job_id is not None:
                # Harbor rewrites job/result.json in place on parallel trial hooks.
                # The public hook config carries the durable ID without that race.
                job_id = str(live_job_id)
            else:
                job = JobResult.model_validate(read_json(root, "result.json"))
                job_id = str(job.id)
                if trial_name is None and not job.finished_at:
                    raise ArtifactError("job_not_finished")
            with _manifest(root, settings.destination) as state:
                if trial_name is not None:
                    names = [trial_name]
                else:
                    names = []
                    with os.scandir(root) as entries:
                        for entry in entries:
                            if len(names) >= 50000:
                                raise ArtifactError("job_too_large")
                            if not entry.name.startswith(".") and entry.is_dir(
                                follow_symlinks=False
                            ):
                                names.append(entry.name)
                            elif entry.is_symlink():
                                raise ArtifactError("symlink_trial")
                    names.sort()
                for name in names:
                    try:
                        trial_id, payload, incomplete, digest = convert_trial(
                            root, name, job_id, settings
                        )
                        previous = _acknowledged(state, trial_id)
                        if previous is not None:
                            if previous != digest:
                                summary.conflicts += 1
                                errors["artifact_conflict"] += 1
                            else:
                                summary.skipped += 1
                            continue
                        send(payload, settings)
                        _acknowledge(state, trial_id, digest)
                        summary.sent += 1
                        summary.incomplete += int(incomplete)
                    except DeliveryError as exc:
                        summary.failed += 1
                        errors[str(exc)] += 1
                    except (OSError, ValueError, TypeError, KeyError, OverflowError):
                        summary.failed += 1
                        errors["invalid_artifact_or_state"] += 1
    except ArtifactError as exc:
        summary.failed += 1
        errors[str(exc)] += 1
    except OSError as exc:
        summary.failed += 1
        errors[f"job_io_errno_{exc.errno}"] += 1
    except (ValueError, TypeError, KeyError):
        summary.failed += 1
        errors["invalid_job_or_state"] += 1
    summary.errors = dict(errors)
    return summary
