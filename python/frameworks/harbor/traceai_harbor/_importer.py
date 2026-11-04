"""Shared finalization/backfill path with local acknowledgement bookkeeping."""

from __future__ import annotations

import fcntl
import hashlib
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
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.trace.v1.trace_pb2 import Span

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


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def _span_hash(span: Span) -> str:
    # The incomplete marker on the already-delivered root cannot be updated when
    # a trajectory recovers. Ignore only that known, documented transition.
    stable = Span()
    stable.CopyFrom(span)
    if span.name == "Harbor trial":
        attrs = [a for a in stable.attributes if a.key != "harbor.trace.incomplete"]
        stable.ClearField("attributes")
        stable.attributes.extend(attrs)
    return hashlib.sha256(stable.SerializeToString(deterministic=True)).hexdigest()


def _acknowledged(state: int, trial_id: str) -> dict | None:
    try:
        manifest = read_json(state, trial_id + ".json")
    except FileNotFoundError:
        return None
    if not isinstance(manifest, dict) or manifest.get("version") not in {1, 2, 3}:
        raise ArtifactError("invalid_manifest")
    if not _is_digest(manifest.get("digest")):
        raise ArtifactError("invalid_manifest")
    if manifest["version"] in {2, 3}:
        ids = manifest.get("span_ids")
        if (
            not _is_digest(manifest.get("result_digest"))
            or not isinstance(ids, list)
            or len(ids) > 8192
            or len(set(map(str, ids))) != len(ids)
            or any(
                not isinstance(sid, str)
                or len(sid) != 16
                or any(c not in "0123456789abcdef" for c in sid)
                for sid in ids
            )
        ):
            raise ArtifactError("invalid_manifest")
        if manifest["version"] == 3:
            hashes = manifest.get("span_hashes")
            if (
                not isinstance(hashes, dict)
                or set(hashes) != set(ids)
                or not all(_is_digest(h) for h in hashes.values())
            ):
                raise ArtifactError("invalid_manifest")
    return manifest


def _acknowledge(
    state: int,
    trial_id: str,
    digest: str,
    *,
    incomplete: bool = False,
    result_digest: str = "",
    span_hashes: dict[str, str] | None = None,
) -> None:
    temporary = ".pending-" + uuid4().hex
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=state,
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            manifest = {"version": 1, "digest": digest}
            if incomplete:
                manifest = {
                    "version": 3,
                    "digest": digest,
                    "result_digest": result_digest,
                    "span_ids": sorted(span_hashes or {}),
                    "span_hashes": span_hashes,
                }
            stream.write(json.dumps(manifest).encode())
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
                materialized = 0
                for name in names:
                    try:
                        trial_id, payload, incomplete, digest, result_digest = (
                            convert_trial(root, name, job_id, settings)
                        )
                        materialized += 1
                        summary.incomplete += int(incomplete)
                        previous = _acknowledged(state, trial_id)
                        if previous is not None and previous["digest"] == digest:
                            summary.skipped += 1
                            continue
                        if previous is not None and (
                            previous["version"] != 3
                            or previous["result_digest"] != result_digest
                        ):
                            # Version 2 recorded IDs without their content. A
                            # changed artifact cannot be safely reconciled.
                            summary.conflicts += 1
                            errors["artifact_conflict"] += 1
                            continue
                        # An incomplete envelope has already been accepted. On
                        # recovery, deliver only newly reconstructed span IDs;
                        # never replay or mutate previously accepted spans.
                        request = ExportTraceServiceRequest.FromString(payload)
                        seen = previous["span_hashes"] if previous else {}
                        current = {
                            s.span_id.hex(): _span_hash(s)
                            for resource in request.resource_spans
                            for scope in resource.scope_spans
                            for s in scope.spans
                        }
                        if len(current) != sum(
                            len(scope.spans)
                            for resource in request.resource_spans
                            for scope in resource.scope_spans
                        ):
                            raise ArtifactError("duplicate_span_id")
                        if any(current.get(sid) != digest for sid, digest in seen.items()):
                            summary.conflicts += 1
                            errors["artifact_conflict"] += 1
                            continue
                        new_count = 0
                        for resource in request.resource_spans:
                            for scope in resource.scope_spans:
                                fresh = [
                                    s for s in scope.spans if s.span_id.hex() not in seen
                                ]
                                new_count += len(fresh)
                                scope.ClearField("spans")
                                scope.spans.extend(fresh)
                        if new_count:
                            send(
                                request.SerializeToString(deterministic=True), settings
                            )
                            summary.sent += 1
                        else:
                            summary.skipped += 1
                        _acknowledge(
                            state,
                            trial_id,
                            digest,
                            incomplete=incomplete,
                            result_digest=result_digest,
                            span_hashes=current,
                        )
                    except DeliveryError as exc:
                        summary.failed += 1
                        errors[str(exc)] += 1
                    except (OSError, ValueError, TypeError, KeyError, OverflowError):
                        summary.failed += 1
                        errors["invalid_artifact_or_state"] += 1
                if trial_name is None and job.stats.n_completed_trials > materialized:
                    missing = job.stats.n_completed_trials - materialized
                    summary.failed += missing
                    errors["missing_completed_trial_artifact"] += missing
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
