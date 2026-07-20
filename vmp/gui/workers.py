"""Qt worker objects for long-running pipeline work."""

from __future__ import annotations

import copy
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from ..core.logging_config import get_logger
from ..core.models import AnalysisResult, AppSettings, ApplyItemUpdate, MediaItem, MediaPlan, PipelineProgress
from ..pipeline import apply_plans, maintain_jpegs, scan_and_plan, scan_items_and_plan

LOGGER = get_logger(__name__)


class ScanWorker(QObject):
    """Worker that scans a root folder and builds a dry-run plan."""

    progress = pyqtSignal(object)
    partial = pyqtSignal(object, object)
    finished = pyqtSignal(object, object)
    failed = pyqtSignal(str)

    def __init__(self, root: Path, settings: AppSettings, items: list[MediaItem] | None = None) -> None:
        super().__init__()
        self._root = root
        self._settings = settings
        self._items = items

    @pyqtSlot()
    def run(self) -> None:
        """Run the scan task."""
        try:
            LOGGER.info("ScanWorker started for root=%s", self._root)
            if self._items is None:
                results, plans = scan_and_plan(
                    self._root,
                    self._settings,
                    self._emit_progress,
                    self._emit_partial,
                    self._cancel_requested,
                )
            else:
                results, plans = scan_items_and_plan(
                    self._items,
                    self._settings,
                    self._emit_progress,
                    self._emit_partial,
                    self._cancel_requested,
                )
            LOGGER.info("ScanWorker finished with results=%s plans=%s", len(results), len(plans))
            self.finished.emit(results, plans)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("ScanWorker failed")
            self.failed.emit(str(exc))

    def _emit_progress(self, event: PipelineProgress) -> None:
        """Forward progress events to Qt."""
        self.progress.emit(event)

    def _emit_partial(self, results: list[AnalysisResult], plans: list[MediaPlan]) -> None:
        """Forward partial scan results to Qt."""
        self.partial.emit(results, plans)

    def _cancel_requested(self) -> bool:
        """Return the interruption state of the worker's current QThread."""
        thread = self.thread()
        return thread is not None and thread.isInterruptionRequested()


class ApplyWorker(QObject):
    """Worker that applies a prepared media plan."""

    progress = pyqtSignal(object)
    item_updated = pyqtSignal(object)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, root: Path, plans: list[MediaPlan], settings: AppSettings) -> None:
        super().__init__()
        self._root = root
        self._plans = plans
        self._settings = settings

    @pyqtSlot()
    def run(self) -> None:
        """Run the apply task."""
        try:
            # Work on an isolated deep copy of the plans. The GUI thread mutates
            # the original plan objects in on_apply_item_updated (final_path,
            # status, item.path) as per-file updates arrive, while this worker
            # thread still reads the plans for the end-of-run manifest/readback.
            # Results flow back via ApplyItemUpdate (by path), not identity, so
            # nothing depends on sharing the instances. Copying here — on the
            # worker thread — keeps the click that starts the apply instant;
            # no update can arrive before run() emits the first one itself.
            self._plans = copy.deepcopy(self._plans)
            LOGGER.info("ApplyWorker started for root=%s plans=%s", self._root, len(self._plans))
            report = apply_plans(
                self._root,
                self._plans,
                self._settings,
                self._emit_progress,
                self._emit_item_update,
                self._cancel_requested,
            )
            LOGGER.info(
                "ApplyWorker finished run_id=%s changed=%s skipped=%s errors=%s",
                report.run_id,
                report.changed,
                report.skipped,
                len(report.errors),
            )
            self.finished.emit(report)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("ApplyWorker failed")
            self.failed.emit(str(exc))

    def _emit_progress(self, event: PipelineProgress) -> None:
        """Forward progress events to Qt."""
        self.progress.emit(event)

    def _emit_item_update(self, update: ApplyItemUpdate) -> None:
        """Forward per-file apply updates to Qt."""
        self.item_updated.emit(update)

    def _cancel_requested(self) -> bool:
        """Return the interruption state of the worker's current QThread."""
        thread = self.thread()
        return thread is not None and thread.isInterruptionRequested()


class JpegMaintenanceWorker(QObject):
    """Worker that repairs JPEG EXIF thumbnails and orientation."""

    progress = pyqtSignal(object)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, root: Path, settings: AppSettings) -> None:
        super().__init__()
        self._root = root
        self._settings = settings

    @pyqtSlot()
    def run(self) -> None:
        """Run the JPEG maintenance task."""
        try:
            LOGGER.info("JpegMaintenanceWorker started for root=%s", self._root)
            report = maintain_jpegs(
                self._root,
                self._settings,
                self._emit_progress,
                self._cancel_requested,
            )
            LOGGER.info(
                "JpegMaintenanceWorker finished run_id=%s changed=%s skipped=%s errors=%s",
                report.run_id,
                report.changed,
                report.skipped,
                len(report.errors),
            )
            self.finished.emit(report)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("JpegMaintenanceWorker failed")
            self.failed.emit(str(exc))

    def _emit_progress(self, event: PipelineProgress) -> None:
        """Forward progress events to Qt."""
        self.progress.emit(event)

    def _cancel_requested(self) -> bool:
        """Return the interruption state of the worker's current QThread."""
        thread = self.thread()
        return thread is not None and thread.isInterruptionRequested()


__all__ = [
    "ScanWorker",
    "ApplyWorker",
    "JpegMaintenanceWorker",
]
