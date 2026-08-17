"""Launching of the Trip Lasso and pair cleanup overlays from the main window (mixin)."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QDialog, QMessageBox

from ...core.discovery import normalize_root
from ...core.i18n import tr
from ...core.logging_config import get_logger
from ...core.settings import save_settings
from ...gps_repair import GpsAssignment, GpsRepairReport, record_from_result
from ...metadata import analyze_item, gps_coordinates
from ...pair_cleanup import find_pairs
from ..lasso.dialog import LassoDialog
from ..lasso.map_view import webengine_available
from ..lasso.trip_selection import TripRecord
from ..pairs.dialog import PairCleanupDialog
from ..gps.dialog import GpsRepairDialog
from ..workers import GpsRepairWorker

LOGGER = get_logger(__name__)


class OverlayFlowMixin:
    """Opens the map-lasso and pair-cleanup overlays and folds their results back in."""

    def _build_trip_records(self) -> list[TripRecord]:
        """Build lightweight selection records from the scanned results."""
        records: list[TripRecord] = []
        for result in self.results:
            coords = gps_coordinates(result.metadata.tags)
            lat, lon = coords if coords is not None else (None, None)
            records.append(
                TripRecord(
                    path=result.item.path,
                    lat=lat,
                    lon=lon,
                    local_dt=result.resolved.local_dt,
                    date_only=result.resolved.local_date_only,
                    kind=result.item.kind.value,
                )
            )
        return records

    def open_trip_lasso(self) -> None:
        """Open the Trip Lasso overlay to select and move media by map/date."""
        if self._has_running_work():
            QMessageBox.information(
                self, tr("Reise-Lasso"), tr("Bitte warten, bis die laufende Verarbeitung abgeschlossen ist.")
            )
            return
        if not self.results:
            return
        if not webengine_available():
            QMessageBox.warning(
                self,
                tr("Reise-Lasso"),
                tr(
                    "Die Karte benötigt QtWebEngine. Bitte 'PyQt6-WebEngine' installieren "
                    "(pip install PyQt6-WebEngine)."
                ),
            )
            return
        records = self._build_trip_records()
        dialog = LassoDialog(
            self,
            records,
            self.root,
            self.settings_model.tools.ffmpeg,
            geometry=self.settings_model.lasso_window_geometry,
            load_target_after_move=self.settings_model.lasso_load_target_after_move,
            thumbnail_cache_mode=self.settings_model.lasso_thumbnail_cache_mode,
            thumbnail_workers=self.settings_model.lasso_thumbnail_workers,
            thumbnail_display_size=self.settings_model.lasso_thumbnail_display_size,
            map_settings=self.settings_model.maps,
        )
        dialog.activePathsRemoved.connect(lambda paths: self._remove_active_paths([Path(path) for path in paths], tr("Reise-Lasso")))
        result = dialog.exec()
        # Remember the overlay's size/position (and maximized state) for next time.
        self.settings_model.lasso_window_geometry = dialog.result_geometry()
        self.settings_model.lasso_load_target_after_move = dialog.load_target_checkbox.isChecked()
        self.settings_model.lasso_thumbnail_display_size = dialog.thumbnail_display_size()
        save_settings(self.settings_model)
        if result == QDialog.DialogCode.Accepted and dialog.moved_sources and not dialog.copied:
            if dialog.load_target_after_move_requested and dialog.target_dir is not None:
                try:
                    normalized = normalize_root(dialog.target_dir)
                except Exception as exc:  # noqa: BLE001
                    QMessageBox.warning(self, tr("Reise-Lasso"), tr("Zielordner konnte nicht geladen werden:\n{error}").format(error=exc))
                    self._remove_moved_media(dialog.moved_sources)
                else:
                    self._reset_for_new_root(normalized)
                    self._discover_and_scan(normalized)
            else:
                self._remove_moved_media(dialog.moved_sources)

    def _remove_moved_media(self, moved: list[Path]) -> None:
        """Drop moved media from the active table-backed pipeline."""
        removed = self._remove_active_paths(moved, tr("Reise-Lasso"))
        if removed:
            LOGGER.info("Trip Lasso removed %s moved media; %s remaining", removed, len(self.results))

    def open_pair_cleanup(self) -> None:
        """Open the IMG_/IMG_E pair cleanup overlay."""
        if self._has_running_work():
            QMessageBox.information(
                self, tr("Paare aufräumen"), tr("Bitte warten, bis die laufende Verarbeitung abgeschlossen ist.")
            )
            return
        if not self.results:
            return
        pairs = find_pairs(self.results)
        if not pairs:
            QMessageBox.information(
                self, tr("Paare aufräumen"), tr("Keine IMG_/IMG_E-Paare mit gleichem Aufnahmedatum gefunden.")
            )
            return
        dialog = PairCleanupDialog(
            self,
            pairs,
            self.root,
            self.settings_model.tools.ffmpeg,
            geometry=self.settings_model.pair_window_geometry,
            workers=self.settings_model.pair_check_workers,
            viewer_geometry=self.settings_model.pair_viewer_geometry,
        )
        dialog.exec()
        # Remember the overlay's and the side-by-side viewer's size/position.
        self.settings_model.pair_window_geometry = dialog.result_geometry()
        self.settings_model.pair_viewer_geometry = dialog.viewer_geometry()
        save_settings(self.settings_model)
        deleted_paths = list(dialog.deleted_paths)
        # Parented dialogs live until the main window dies; release explicitly.
        dialog.deleteLater()
        if deleted_paths:
            removed = self._remove_active_paths(deleted_paths, tr("Paare aufräumen"))
            LOGGER.info("Pair cleanup removed %s duplicates; %s remaining", removed, len(self.results))
            self._update_pairs_badge()

    def open_gps_repair(self) -> None:
        """Open the unified automatic/manual missing-GPS repair dialog."""
        if self._has_running_work():
            QMessageBox.information(
                self,
                tr("GPS ergänzen"),
                tr("Bitte warte, bis die laufende Verarbeitung abgeschlossen ist."),
            )
            return
        records = [record_from_result(result) for result in self.results]
        if not any(not record.has_gps for record in records):
            QMessageBox.information(self, tr("GPS ergänzen"), tr("Alle gescannten Dateien enthalten GPS-Positionen."))
            return
        dialog = GpsRepairDialog(
            self,
            records,
            self.settings_model.gps_repair,
            geometry=self.settings_model.gps_repair_window_geometry,
            ffmpeg=self.settings_model.tools.ffmpeg,
            thumbnail_workers=self.settings_model.lasso_thumbnail_workers,
            thumbnail_cache_mode=self.settings_model.lasso_thumbnail_cache_mode,
            map_settings=self.settings_model.maps,
        )
        self._gps_repair_dialog = dialog
        dialog.applyRequested.connect(self._start_gps_repair)
        dialog.exec()
        self.settings_model.gps_repair_window_geometry = dialog.result_geometry()
        save_settings(self.settings_model)
        self._gps_repair_dialog = None
        dialog.deleteLater()

    def _start_gps_repair(self, assignments: list[GpsAssignment], create_backups: bool) -> None:
        """Start the standalone writer in the main window's single worker slot."""
        dialog = getattr(self, "_gps_repair_dialog", None)
        if self._has_running_work():
            if dialog is not None:
                dialog.set_busy(False)
            self._block_if_busy()
            return
        self._set_busy(True)
        self.progress.setValue(0)
        self.status_label.setText(tr("GPS-Daten werden geschrieben …"))
        worker = GpsRepairWorker(assignments, self.settings_model, create_backups)
        if dialog is not None:
            worker.failed.connect(lambda _message: dialog.set_busy(False))
        self._start_worker(worker, self._gps_repair_finished)

    def _gps_repair_finished(self, report: GpsRepairReport) -> None:
        """Fold verified GPS readbacks into the active results and table."""
        LOGGER.info(
            "GPS repair result received run_id=%s entries=%s changed=%s failed=%s",
            report.run_id,
            len(report.entries),
            report.changed,
            report.failed,
        )
        dialog = getattr(self, "_gps_repair_dialog", None)
        try:
            result_indices = {result.item.path.resolve(): index for index, result in enumerate(self.results)}
            plan_rows = {plan.analysis.item.path.resolve(): row for row, plan in enumerate(self.plans)}
            for entry in report.entries:
                if entry.readback is None:
                    continue
                key = entry.assignment.target.path.resolve()
                index = result_indices.get(key)
                if index is None:
                    continue
                old_result = self.results[index]
                refreshed = analyze_item(old_result.item, entry.readback, self.settings_model.metadata)
                self.results[index] = refreshed
                row = plan_rows.get(key)
                if row is not None:
                    self.plans[row].analysis = refreshed
                    self._refresh_table_row(row)
                if entry.backup_path is not None:
                    self._backup_paths[old_result.item.path] = entry.backup_path
            self._apply_table_filters()
            self._update_missing_gps_badge()
            self._set_busy(False)
            self.status_label.setText(
                tr("GPS-Reparatur: {changed} geschrieben, {failed} Fehler.").format(
                    changed=report.changed, failed=report.failed
                )
            )
        except Exception as exc:  # noqa: BLE001 - never let a Qt result slot abort the process
            LOGGER.exception("Could not apply GPS repair result in the GUI")
            self._set_busy(False)
            if dialog is not None:
                dialog.set_busy(False)
            message = str(exc)
            QTimer.singleShot(0, lambda: QMessageBox.critical(self, tr("Fehler"), message))
            return

        # Do not start a nested QMessageBox event loop while handling the
        # worker's finished signal. Returning first lets QThread.quit and its
        # deferred cleanup complete without re-entering the completion chain.
        if dialog is not None:
            QTimer.singleShot(0, lambda: self._deliver_gps_repair_report(dialog, report))

    @staticmethod
    def _deliver_gps_repair_report(dialog: GpsRepairDialog, report: GpsRepairReport) -> None:
        """Deliver the dialog update outside the worker completion signal."""
        try:
            dialog.apply_report(report)
        except Exception:  # noqa: BLE001 - Qt callbacks must never escape into qFatal
            LOGGER.exception("Could not display GPS repair result")
            try:
                dialog.set_busy(False)
            except RuntimeError:
                LOGGER.debug("GPS repair dialog was already deleted")

