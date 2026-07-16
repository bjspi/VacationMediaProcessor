"""Apply-run orchestration and post-apply bookkeeping for the main window (mixin)."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSlot
from PyQt6.QtWidgets import QMessageBox

from ...core.discovery import normalize_root
from ...core.i18n import tr
from ..common.file_transfer import path_key, same_path
from ..workers import ApplyWorker, JpegMaintenanceWorker
from ...core.logging_config import get_logger
from ...core.models import ApplyItemUpdate, MediaKind, MediaPlan, PipelineReport, PlanStatus

LOGGER = get_logger(__name__)


class ApplyFlowMixin:
    """Run buttons, apply worker lifecycle, and per-item plan updates for MainWindow."""

    def _backup_confirmation(self, title: str, question: str, scope: str) -> tuple[str, str]:
        """Return a confirmation title and message for backup-sensitive runs."""
        if self.settings_model.skip_backup:
            return (
                tr("{title} ohne Backup").format(title=title),
                tr("{question}\nACHTUNG: Backup ist deaktiviert. Originaldateien können {scope} ohne Sicherung ersetzt, verschoben oder gelöscht werden.").format(question=question, scope=scope),
            )
        return title, tr("{question}\nOriginale werden vorher in den Backup-Ordner kopiert.").format(question=question)

    def run_all(self) -> None:
        """Apply the full prepared plan, skipping already-processed (DONE) files."""
        if self.root is None or not self.plans:
            LOGGER.warning("Run all ignored root=%s plans=%s", self.root, len(self.plans))
            return
        plans = [plan for plan in self.plans if plan.analysis.status != PlanStatus.DONE]
        LOGGER.info("Run all requested plans=%s (filtered %s DONE)", len(plans), len(self.plans) - len(plans))
        if not plans:
            QMessageBox.information(self, tr("Nichts zu tun"), tr("Alle Dateien wurden bereits verarbeitet (DONE)."))
            return
        self.run_plans(plans, tr("Alle aktiv geplanten Schritte ausführen?"))

    def run_images(self) -> None:
        """Apply prepared plans for image files, skipping already-processed (DONE) files."""
        plans = [
            plan
            for plan in self.plans
            if plan.analysis.item.kind == MediaKind.IMAGE and plan.analysis.status != PlanStatus.DONE
        ]
        LOGGER.info("Run images requested image_plans=%s total_plans=%s", len(plans), len(self.plans))
        if not plans:
            QMessageBox.information(self, tr("Nichts zu tun"), tr("Alle Bild-Dateien wurden bereits verarbeitet (DONE)."))
            return
        self.run_plans(plans, tr("Alle geplanten Bild-Schritte ausführen?"))

    def run_videos(self) -> None:
        """Apply prepared plans for video files, skipping already-processed (DONE) files."""
        plans = [
            plan
            for plan in self.plans
            if plan.analysis.item.kind == MediaKind.VIDEO and plan.analysis.status != PlanStatus.DONE
        ]
        LOGGER.info("Run videos requested video_plans=%s total_plans=%s", len(plans), len(self.plans))
        if not plans:
            QMessageBox.information(self, tr("Nichts zu tun"), tr("Alle Video-Dateien wurden bereits verarbeitet (DONE)."))
            return
        self.run_plans(plans, tr("Alle geplanten Video-Schritte ausführen?"))

    def run_selected(self) -> None:
        """Apply prepared plans for selected rows only."""
        rows = self._selected_visible_rows()
        plans = [self.plans[row] for row in rows if 0 <= row < len(self.plans)]
        LOGGER.info("Run selected requested rows=%s plans=%s", rows, len(plans))
        self.run_plans(plans, tr("{count} ausgewählte Datei(en) verarbeiten?").format(count=len(plans)))

    def run_jpeg_maintenance(self) -> None:
        """Run the standalone JPEG orientation/thumbnail maintenance workflow."""
        if self.root is None:
            LOGGER.warning("JPEG maintenance requested without root")
            QMessageBox.warning(self, tr("Kein Ordner"), tr("Bitte zuerst einen Ordner öffnen."))
            return
        if self._block_if_busy():
            return
        self._sync_settings_from_ui()
        LOGGER.info("JPEG maintenance requested root=%s xnconvert=%s", self.root, self.settings_model.tools.xnconvert)
        if not self.settings_model.images.jpeg_rotate_by_exif and not self.settings_model.images.jpeg_rebuild_exif_thumbnail:
            LOGGER.warning("JPEG maintenance blocked because no option is enabled")
            QMessageBox.information(self, "JPG Fix", tr("Bitte mindestens eine JPG-Fix-Option für JPG Fix (EXIF-Rotation/Thumbnail) aktivieren."))
            return
        title, message = self._backup_confirmation(
            "JPG Fix",
            tr("Alle JPG/JPEG-Dateien im gewählten Ordner mit JPG Fix (EXIF-Rotation/Thumbnail) warten?"),
            tr("beim JPG Fix (EXIF-Rotation/Thumbnail)"),
        )
        answer = QMessageBox.question(
            self,
            title,
            message,
        )
        if answer != QMessageBox.StandardButton.Yes:
            LOGGER.info("JPEG maintenance cancelled by user")
            return
        self._set_busy(True)
        self.status_label.setText(tr("JPG Fix (EXIF-Rotation/Thumbnail) läuft..."))
        LOGGER.info("Starting JPEG maintenance worker")
        self._start_worker(JpegMaintenanceWorker(self.root, self.settings_model), self.on_jpeg_maintenance_finished)

    def run_plans(self, plans: list[MediaPlan], question: str) -> None:
        """Apply a provided set of plans."""
        if self.root is None or not plans:
            LOGGER.warning("Run plans ignored root=%s plans=%s", self.root, len(plans))
            QMessageBox.information(self, tr("Nichts ausgewählt"), tr("Keine passenden Pläne ausgewählt."))
            return
        if self._block_if_busy():
            return
        requested_paths = [path_key(plan.analysis.item.path) for plan in plans]
        # Sidebar changes are debounced for responsive editing. Apply must flush
        # that pending rebuild synchronously; otherwise an old conversion plan
        # can run with the just-changed settings.
        self._workflow_refresh_timer.stop()
        self._apply_workflow_refresh()
        if self.results:
            current_by_path = {path_key(plan.analysis.item.path): plan for plan in self.plans}
            plans = [current_by_path[key] for key in requested_paths if key in current_by_path]
        if not plans:
            LOGGER.warning("Run plans became empty after synchronizing the workflow plan")
            QMessageBox.information(self, tr("Nichts ausgewählt"), tr("Keine passenden Pläne ausgewählt."))
            return
        actionable_count = sum(1 for plan in plans if plan.actions)
        LOGGER.info(
            "Run plans requested plans=%s actionable=%s question=%s exiftool=%s xnconvert=%s ffmpeg=%s",
            len(plans),
            actionable_count,
            question,
            self.settings_model.tools.exiftool,
            self.settings_model.tools.xnconvert,
            self.settings_model.tools.ffmpeg,
        )
        title, message = self._backup_confirmation("Apply", question, tr("in diesem Lauf"))
        answer = QMessageBox.question(
            self,
            title,
            message,
        )
        if answer != QMessageBox.StandardButton.Yes:
            LOGGER.info("Run plans cancelled by user")
            return
        self._applied_plans = list(plans)
        self._applied_plan_sources = {id(plan): plan.analysis.item.path for plan in plans}
        self._applied_plan_roots = {id(plan): normalize_root(plan.analysis.item.root) for plan in plans}
        self._backup_paths = {}
        self._set_readback_diff_paths(None)
        self._original_sizes = {}
        for plan in plans:
            try:
                self._original_sizes[plan.analysis.item.path] = plan.analysis.item.path.stat().st_size
            except OSError:
                pass
        self._rebuild_row_map()
        self._set_busy(True)
        LOGGER.info("Starting apply worker plans=%s", len(plans))
        worker = ApplyWorker(self.root, plans, self.settings_model)
        worker.item_updated.connect(self.on_apply_item_updated)
        self._start_worker(worker, self.on_apply_finished)

    @pyqtSlot(object)
    def on_apply_item_updated(self, update: ApplyItemUpdate) -> None:
        """Apply one finished-file update to its table row without rebuilding the table."""
        row = self._row_for_path(update.source_path)
        if row is None:
            LOGGER.warning("Apply item update could not find row for source=%s", update.source_path)
            return
        plan = self.plans[row]
        old_path = plan.analysis.item.path
        final_path = (update.final_path or old_path) if update.changed else old_path
        plan.final_path = final_path
        if update.changed:
            plan.analysis.status = PlanStatus.DONE
        elif update.errors or update.skipped:
            plan.analysis.status = PlanStatus.SKIP
        if update.original_size is not None:
            self._original_sizes[update.source_path] = update.original_size
            self._original_sizes[old_path] = update.original_size
            self._original_sizes[final_path] = update.original_size
        if update.current_size is not None:
            # The size column reads from disk, but keeping the original size
            # keyed by the final path lets it show before/after deltas.
            self._original_sizes.setdefault(final_path, update.original_size or update.current_size)
        if update.backup_path is not None and update.backup_path.exists():
            self._backup_paths[update.source_path.resolve()] = update.backup_path
            self._backup_paths[final_path.resolve()] = update.backup_path
        if update.changed and not same_path(final_path, old_path):
            LOGGER.info("Apply item update path: %s -> %s", old_path, final_path)
            plan.analysis.item.path = final_path
            plan.analysis.metadata.source_file = str(final_path)
            for action in plan.actions:
                if same_path(action.source, old_path):
                    action.source = final_path
                if action.target is not None and same_path(action.target, old_path):
                    action.target = final_path
        self._row_by_path[path_key(update.source_path)] = row
        self._row_by_path[path_key(final_path)] = row
        self._refresh_table_row(row)
        self._schedule_stats_update()
        selected_rows = set(self._selected_visible_rows())
        if row in selected_rows:
            self.update_selection()

    def on_apply_finished(self, report: PipelineReport) -> None:
        """Show apply report."""
        self._set_busy(False)
        self._last_apply_run_id = report.run_id
        LOGGER.info(
            "Apply finished in GUI run_id=%s changed=%s skipped=%s errors=%s",
            report.run_id,
            report.changed,
            report.skipped,
            len(report.errors),
        )
        self.run_button.setEnabled(False)
        self.run_images_button.setEnabled(False)
        self.run_videos_button.setEnabled(False)
        self.run_selected_button.setEnabled(False)
        self.status_label.setText(tr("Apply abgeschlossen."))
        message = tr(
            "Run: {run_id}\n"
            "Geändert: {changed}\n"
            "Übersprungen: {skipped}\n"
            "Warnungen: {warnings}\n"
            "Fehler: {errors}"
        ).format(
            run_id=report.run_id,
            changed=report.changed,
            skipped=report.skipped,
            warnings=len(report.warnings),
            errors=len(report.errors),
        )
        if report.errors:
            message += "\n\n" + "\n".join(report.errors[:10])
        self._set_readback_diff_paths(self._readback_manifest_paths_for_run(report.run_id))
        QMessageBox.information(self, tr("Fertig"), message)
        self._refresh_backup_paths(report.run_id)
        self._update_plan_references_after_apply(self._applied_plans)

    def _update_plan_references_after_apply(self, applied_plans: list[MediaPlan] | None = None) -> None:
        """Update internal file references to match post-apply paths on disk.

        After apply, files may have been renamed, converted, or transcoded.
        Instead of re-scanning (which would re-enable Apply on DONE files),
        we update the in-memory plan/item paths to point to the final_path
        that was actually written. This keeps preview and double-click working.
        """
        applied_plans = self._applied_plans if applied_plans is None else applied_plans
        LOGGER.info("Updating plan references after apply (%s applied plans)", len(applied_plans))
        for plan in applied_plans:
            final = plan.final_path
            old_path = plan.analysis.item.path
            row = self._row_for_path(self._applied_plan_sources.get(id(plan), old_path))
            if plan.analysis.status == PlanStatus.SKIP:
                if row is not None:
                    self._refresh_table_row(row)
                continue
            plan.analysis.status = PlanStatus.DONE
            if final is None or final.resolve() == old_path.resolve():
                if row is not None:
                    self._refresh_table_row(row)
                continue
            LOGGER.info("Updating reference: %s -> %s", old_path, final)
            plan.analysis.item.path = final
            plan.analysis.metadata.source_file = str(final)
            if old_path in self._original_sizes:
                self._original_sizes[final] = self._original_sizes.pop(old_path)
            for action in plan.actions:
                if action.source == old_path:
                    action.source = final
                if action.target is not None and action.target.resolve() == old_path.resolve():
                    action.target = final
            if row is not None:
                self._row_by_path[path_key(final)] = row
                self._refresh_table_row(row)
        self._applied_plans = []
        self._update_stats()
        self._set_busy(False)

    def on_jpeg_maintenance_finished(self, report: PipelineReport) -> None:
        """Show the JPEG maintenance report."""
        self._set_busy(False)
        self.status_label.setText(tr("JPG Fix (EXIF-Rotation/Thumbnail) abgeschlossen."))
        message = tr(
            "Run: {run_id}\n"
            "Bearbeitet: {changed}\n"
            "Übersprungen: {skipped}\n"
            "Fehler: {errors}"
        ).format(
            run_id=report.run_id,
            changed=report.changed,
            skipped=report.skipped,
            errors=len(report.errors),
        )
        if report.errors:
            message += "\n\n" + "\n".join(report.errors[:10])
        QMessageBox.information(self, tr("JPG Fix fertig"), message)

