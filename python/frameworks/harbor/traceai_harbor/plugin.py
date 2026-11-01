"""Public Harbor lifecycle hooks; no agent/environment changes or monkey-patching."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harbor.job import Job
    from harbor.models.job.result import JobResult
    from harbor.trial.hooks import TrialHookEvent

from harbor.models.job.plugin import BaseJobPlugin

from ._importer import ImportSummary, _import, import_job
from ._transport import Settings

_LOG = logging.getLogger(__name__)


class FutureAGIPlugin(BaseJobPlugin):
    def __init__(
        self, *, project_name: str | None = None, capture_content: bool = False
    ) -> None:
        # Credentials/endpoint are environment-only: Harbor persists plugin kwargs.
        self._settings = Settings.from_env(
            project_name=project_name, capture_content=capture_content
        )
        self._job_dir: Path | None = None
        self._lock = asyncio.Lock()
        self.summary = ImportSummary()

    async def on_job_start(self, job: Job) -> None:
        self._job_dir = job.job_dir
        job.on_trial_ended(self._on_trial_ended)

    async def _on_trial_ended(self, event: TrialHookEvent) -> None:
        if self._job_dir is None:
            return
        # Backpressure instead of an unbounded in-memory queue. At most one transfer
        # per plugin; Harbor bounds concurrent waiting trials. No background flush.
        async with self._lock:
            result = await asyncio.to_thread(
                _import, self._job_dir, self._settings, event.trial_name
            )
        if not result.ok:
            _LOG.warning(
                "Future AGI trial delivery incomplete; run traceai-harbor import-job after the job finishes"
            )

    async def on_job_end(self, job_result: JobResult) -> None:
        if self._job_dir is None:
            return
        async with self._lock:
            self.summary = await asyncio.to_thread(
                import_job, self._job_dir, settings=self._settings
            )
        if not self.summary.ok:
            _LOG.warning(
                "Future AGI job delivery incomplete: %d failed, %d conflicts; run traceai-harbor import-job",
                self.summary.failed,
                self.summary.conflicts,
            )
