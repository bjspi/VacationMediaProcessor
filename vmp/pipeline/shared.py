"""Shared pipeline infrastructure: run ids, directories, progress, errors."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from ..core.logging_config import get_logger
from ..core.models import AnalysisResult, ApplyItemUpdate, MediaPlan, Phase, PipelineProgress
from ..core.processes import resolve_executable

ProgressCallback = Callable[[PipelineProgress], None]
ApplyItemCallback = Callable[[ApplyItemUpdate], None]
ResultsCallback = Callable[[list[AnalysisResult], list[MediaPlan]], None]
CancelCallback = Callable[[], bool]
LOGGER = get_logger(__name__)


class PipelineError(RuntimeError):
    """Raised when the processing pipeline cannot continue safely."""


class PipelineCancelled(PipelineError):
    """Raised when the GUI requests a cooperative pipeline cancellation."""


class VideoNotSmallerError(PipelineError):
    """Raised when a transcode succeeded but the output is not smaller.

    This is a deliberate no-op, not a failure: the original is kept because the
    re-encode did not save space (``replace_if_larger`` off, file above the
    ``always_replace_below_mb`` threshold). Callers treat it as a skip-with-
    warning rather than an error so it does not inflate the error count.
    """


def make_run_id() -> str:
    """Create a sortable run identifier that does not collide within one second."""
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def raise_if_cancelled(callback: CancelCallback | None) -> None:
    """Abort at a safe pipeline boundary when cancellation was requested."""
    if callback is not None and callback():
        raise PipelineCancelled("Processing was cancelled.")


def work_dir(root: Path, run_id: str) -> Path:
    """Return the work directory for a run."""
    return root / "_VacationMediaProcessor_Work" / run_id


def backup_dir(root: Path, run_id: str) -> Path:
    """Return the backup directory for a run."""
    return root / "_VacationMediaProcessor_Backup" / run_id


def _cleanup_empty_generated_dirs(root: Path, run_id: str) -> None:
    """Remove empty work directories after a run completes."""
    work_root = root / "_VacationMediaProcessor_Work"
    work_run = work_root / run_id
    try:
        if work_run.exists() and work_run.is_dir() and not any(work_run.iterdir()):
            work_run.rmdir()
            LOGGER.info("Removed empty work run directory: %s", work_run)
        if work_root.exists() and work_root.is_dir() and not any(work_root.iterdir()):
            work_root.rmdir()
            LOGGER.info("Removed empty work root directory: %s", work_root)
    except OSError:
        LOGGER.info("Keeping work directories because they are not empty: %s", work_root)


def emit(callback: ProgressCallback | None, phase: Phase, current: int, total: int, message: str) -> None:
    """Emit a progress event when a callback exists."""
    if callback:
        callback(PipelineProgress(phase=phase, current=current, total=total, message=message))


def _resolve_required_tool(label: str, executable: str) -> str:
    """Resolve a required external executable or raise a friendly error."""
    LOGGER.info("Resolving required tool %s executable=%s", label, executable)
    resolved = resolve_executable(executable)
    if resolved is None:
        LOGGER.error("Required tool not found: %s executable=%s", label, executable)
        raise PipelineError(f"{label} was not found: {executable}")
    LOGGER.info("Resolved required tool %s -> %s", label, resolved)
    return resolved

