"""Launching of the Trip Lasso and pair cleanup overlays from the main window (mixin)."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import QDialog, QMessageBox

from ...core.discovery import normalize_root
from ...core.i18n import tr
from ..lasso.dialog import LassoDialog
from ..lasso.map_view import webengine_available
from ..pairs.dialog import PairCleanupDialog
from ..lasso.trip_selection import TripRecord
from ...core.logging_config import get_logger
from ...metadata import gps_coordinates
from ...pair_cleanup import find_pairs
from ...core.settings import save_settings

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

