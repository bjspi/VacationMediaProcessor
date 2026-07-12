"""Worker-thread lifecycle, busy state, and progress handling for the main window (mixin)."""

from __future__ import annotations

from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QMessageBox

from ...core.i18n import tr
from ...core.logging_config import get_logger
from ...core.models import MediaKind, PipelineProgress
from ...core.processes import active_process_count, kill_active_processes

LOGGER = get_logger(__name__)


class WorkerLifecycleMixin:
    """Single-slot worker thread management: start, guard, abort, progress, busy UI."""

    def _start_worker(self, worker, finished_slot) -> None:
        """Move a worker onto a fresh QThread with the standard signal wiring."""
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.on_progress)
        worker.finished.connect(finished_slot)
        worker.failed.connect(self.on_worker_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_worker_refs)
        self.worker_thread = thread
        self.worker = worker
        thread.start()

    def _clear_worker_refs(self) -> None:
        """Release finished worker references and continue queued folder drops."""
        LOGGER.info("Worker thread finished, clearing refs")
        self.worker_thread = None
        self.worker = None
        self._process_pending_folders()

    def _has_running_work(self) -> bool:
        """Return whether a worker thread or child process is still active."""
        return bool(self.worker_thread and self.worker_thread.isRunning()) or active_process_count() > 0

    def _block_if_busy(self) -> bool:
        """Warn and return True when a scan/apply/maintenance run is still active.

        A single worker slot cannot represent two concurrent runs, so every entry
        point that would start a new worker (scan, apply, JPEG fix, opening a
        folder) must refuse while one is live. Without this, a second run would
        overwrite ``self.worker_thread`` and orphan the first thread and its child
        ffmpeg/nconvert processes.
        """
        if self._has_running_work():
            QMessageBox.information(
                self,
                tr("Verarbeitung läuft"),
                tr(
                    "Es läuft gerade noch eine Verarbeitung.\n\n"
                    "Bitte warte, bis der aktuelle Vorgang abgeschlossen ist, "
                    "bevor du einen neuen Ordner öffnest oder einen weiteren Lauf startest."
                ),
            )
            return True
        return False

    def _abort_running_work(self) -> None:
        """Forcefully stop active child processes and let the worker unwind."""
        killed = kill_active_processes()
        LOGGER.warning("Abort requested: killed %s external process(es)", killed)
        if self.worker_thread is None:
            return
        self.worker_thread.requestInterruption()
        self.worker_thread.quit()
        if not self.worker_thread.wait(2000):
            LOGGER.warning("Worker thread still running after abort wait, terminating it")
            self.worker_thread.terminate()
            self.worker_thread.wait(1000)

    def on_progress(self, event: PipelineProgress) -> None:
        """Update progress controls."""
        total = max(event.total, 1)
        self.progress.setValue(int(event.current / total * 100))
        self.status_label.setText(f"{event.phase.value}: {event.message}")
        LOGGER.info("Progress phase=%s current=%s total=%s message=%s", event.phase.value, event.current, event.total, event.message)

    def on_worker_failed(self, message: str) -> None:
        """Show worker failure."""
        self._set_busy(False)
        if self._pending_folders:
            LOGGER.warning("Dropping %s queued folder(s) after worker failure", len(self._pending_folders))
            self._pending_folders.clear()
        LOGGER.error("Worker failed: %s", message)
        QMessageBox.critical(self, tr("Fehler"), message)
        self.status_label.setText(tr("Fehler."))

    def _set_busy(self, busy: bool) -> None:
        """Enable or disable controls during long-running work."""
        if hasattr(self, "open_action"):
            self.open_action.setEnabled(not busy)
        self.scan_button.setEnabled(not busy)
        self.jpeg_fix_button.setEnabled(not busy and self.root is not None)
        actionable_plans = [plan for plan in self.plans if plan.actions]
        self.run_button.setEnabled(not busy and bool(actionable_plans))
        self.run_images_button.setEnabled(not busy and any(plan.analysis.item.kind == MediaKind.IMAGE for plan in actionable_plans))
        self.run_videos_button.setEnabled(not busy and any(plan.analysis.item.kind == MediaKind.VIDEO for plan in actionable_plans))
        self.run_selected_button.setEnabled(not busy and bool(actionable_plans))
        self.export_button.setEnabled(not busy and bool(actionable_plans))
        self.missing_button.setEnabled(not busy and bool(actionable_plans))
        self.lasso_button.setEnabled(not busy and bool(self.results))
        self.pairs_button.setEnabled(not busy and bool(self.results))

