"""Background download jobs for the web API: a small thread pool plus in-memory job state."""

import logging
import threading
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from clipforage import config, service

logger = logging.getLogger("clipforage")

MAX_WORKERS = 2
MAX_JOBS = 100
_FINISHED = ("done", "error")


@dataclass
class Job:
    """Mutable state of one download; read it via ``JobManager.get`` / ``list`` snapshots."""

    job_id: str
    ref: str | None
    url: str | None
    kind: str = "download"  # "download" or "keyout"
    status: str = "queued"
    progress: float | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    path: str | None = None
    sidecar: str | None = None
    license: str | None = None
    attribution_text: str | None = None
    error: str | None = None
    history_id: int | None = None
    stage: str | None = None  # "postprocessing" while yt-dlp merges / extracts audio
    title: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class JobManager:
    """Runs downloads on a ``ThreadPoolExecutor`` and keeps the last ``max_jobs`` jobs."""

    def __init__(self, max_workers: int = MAX_WORKERS, max_jobs: int = MAX_JOBS) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="cf-dl")
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._lock = threading.Lock()
        self._max_jobs = max_jobs

    def submit(
        self,
        *,
        ref: str | None = None,
        url: str | None = None,
        mp3: bool = False,
        clip: tuple[float, float] | None = None,
        max_wall_s: float | None = None,
        quality: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Queue a download of ``ref`` or ``url`` into ``config.library_dir()``; return a snapshot.

        ``max_wall_s`` only applies to ``ref`` downloads (http clip watchdog); ``url`` ignores it.
        ``quality`` (max video height) only affects yt-dlp downloads. ``title`` is display-only.
        """
        job = Job(job_id=uuid.uuid4().hex, ref=ref, url=url, title=title)
        with self._lock:
            self._jobs[job.job_id] = job
            self._evict()
            snapshot = asdict(job)
        self._executor.submit(self._run, job, mp3, clip, max_wall_s, quality)
        return snapshot

    def submit_keyout(
        self, history_id: int, tolerance: int = 80, max_wall_s: float | None = None
    ) -> dict[str, Any]:
        """Queue a chroma-key export of history row ``history_id``; return a snapshot.

        The job's ``ref`` is ``#<history_id>``; on success it carries the new ``.key.webm``
        ``path``, ``sidecar`` and ``history_id``.
        """
        job = Job(job_id=uuid.uuid4().hex, ref=f"#{history_id}", url=None, kind="keyout")
        with self._lock:
            self._jobs[job.job_id] = job
            self._evict()
            snapshot = asdict(job)
        self._executor.submit(self._run_keyout, job, history_id, tolerance, max_wall_s)
        return snapshot

    def get(self, job_id: str) -> dict[str, Any] | None:
        """Return a snapshot of one job, or None if unknown (or evicted)."""
        with self._lock:
            job = self._jobs.get(job_id)
            return asdict(job) if job is not None else None

    def list(self) -> list[dict[str, Any]]:
        """Return snapshots of all kept jobs, most recent first."""
        with self._lock:
            return [asdict(job) for job in reversed(self._jobs.values())]

    def shutdown(self, wait: bool = False) -> None:
        """Stop accepting jobs; queued jobs that have not started are cancelled."""
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def _evict(self) -> None:
        """Drop the oldest finished jobs while over the cap (running jobs are kept)."""
        if len(self._jobs) <= self._max_jobs:
            return
        for job_id in [j.job_id for j in self._jobs.values() if j.status in _FINISHED]:
            if len(self._jobs) <= self._max_jobs:
                break
            del self._jobs[job_id]

    def _update(self, job: Job, **changes: Any) -> None:
        with self._lock:
            for name, value in changes.items():
                setattr(job, name, value)

    def _progress_hook(self, job: Job) -> service.ProgressHook:
        def hook(info: dict[str, Any]) -> None:
            if info.get("status") == "postprocessing":
                self._update(job, stage="postprocessing")  # keep the last known progress
                return
            downloaded = info.get("downloaded_bytes")
            total = info.get("total_bytes") or info.get("total_bytes_estimate")
            progress = None
            if isinstance(downloaded, int | float) and isinstance(total, int | float) and total:
                progress = max(0.0, min(1.0, downloaded / total))
            fraction = info.get("fraction")  # ffmpeg http clips report time, not bytes
            if downloaded is None and isinstance(fraction, int | float):
                progress = max(0.0, min(1.0, float(fraction)))
            self._update(
                job,
                downloaded_bytes=int(downloaded) if downloaded is not None else None,
                total_bytes=int(total) if total else None,
                progress=progress,
                **({"stage": None} if info.get("status") == "finished" else {}),
            )

        return hook

    def _run(
        self,
        job: Job,
        mp3: bool,
        clip: tuple[float, float] | None,
        max_wall_s: float | None = None,
        quality: str | None = None,
    ) -> None:
        self._update(job, status="running")
        hook = self._progress_hook(job)
        try:
            out = config.library_dir()
            if job.ref is not None:
                url, result = service.download_ref(
                    job.ref,
                    out=out,
                    mp3=mp3,
                    clip=clip,
                    progress=hook,
                    max_wall_s=max_wall_s,
                    quality=quality,
                )
            else:
                assert job.url is not None
                url = job.url
                result = service.download_link(
                    url, out=out, mp3=mp3, clip=clip, progress=hook, quality=quality
                )
            meta = service.record_history(url, result)
        except Exception as exc:  # noqa: BLE001 - a job failure is reported, never raised
            logger.warning("download job %s failed: %s", job.job_id, exc)
            self._update(job, status="error", error=str(exc))
            return
        self._update(
            job,
            status="done",
            progress=1.0,
            path=str(result.path),
            sidecar=str(result.sidecar),
            license=meta.get("license"),
            attribution_text=meta.get("attribution_text"),
            history_id=meta.get("history_id"),
        )

    def _run_keyout(
        self, job: Job, history_id: int, tolerance: int, max_wall_s: float | None
    ) -> None:
        self._update(job, status="running")
        try:
            meta = service.key_out_download(
                history_id,
                tolerance=tolerance,
                progress=self._progress_hook(job),
                max_wall_s=max_wall_s,
            )
        except Exception as exc:  # noqa: BLE001 - a job failure is reported, never raised
            logger.warning("keyout job %s failed: %s", job.job_id, exc)
            self._update(job, status="error", error=str(exc))
            return
        self._update(
            job,
            status="done",
            progress=1.0,
            path=meta.get("path"),
            sidecar=meta.get("sidecar"),
            license=meta.get("license"),
            attribution_text=meta.get("attribution_text"),
            history_id=meta.get("history_id"),
        )
