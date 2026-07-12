"""Row-level actions on the media table: open, diff, GPS, selection, removal (mixin)."""

from __future__ import annotations

import os
import subprocess
import webbrowser
from pathlib import Path

from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QMenu, QMessageBox

from ...core.i18n import tr
from ..common.file_transfer import path_key, same_path
from ..common.plan_display import details_markdown, video_bucket_label_text
from ..common.widgets import MediaTableColumns
from ...core.logging_config import get_logger
from ...metadata import gps_coordinates
from ...core.models import AnalysisResult, MediaKind, MediaPlan, PlanStatus

LOGGER = get_logger(__name__)


class TableActionsMixin:
    """Double-click/context-menu behaviour, row bookkeeping, and active-list removal."""

    def _on_table_double_clicked(self, row: int, _column: int) -> None:
        """Open the media file with the default OS application."""
        if row < 0 or row >= len(self.plans):
            LOGGER.warning("Double-click on invalid row %s (plans=%s)", row, len(self.plans))
            return
        plan = self.plans[row]
        file_path = plan.analysis.item.path
        if _column == MediaTableColumns.index("gps"):
            self._open_gps_maps(plan)
            return
        if _column == MediaTableColumns.index("size") and self._open_diff_for_row(row):
            return
        self._open_file(file_path)

    def _open_gps_maps(self, plan: MediaPlan) -> None:
        """Open Google Maps at the media file's GPS coordinates."""
        coords = gps_coordinates(plan.analysis.metadata.tags)
        if coords is None:
            QMessageBox.information(
                self,
                "GPS",
                tr("Diese Datei enthält keine GPS-Koordinaten."),
            )
            return
        latitude, longitude = coords
        url = f"https://www.google.com/maps/search/?api=1&query={latitude:.6f},{longitude:.6f}"
        LOGGER.info("Opening Google Maps for %s at %.6f,%.6f", plan.analysis.item.path.name, latitude, longitude)
        webbrowser.open(url)

    def _show_table_context_menu(self, position: QPoint) -> None:
        """Show row-level file and diff actions."""
        index = self.table.indexAt(position)
        if not index.isValid():
            return
        row = index.row()
        if row < 0 or row >= len(self.plans):
            return
        selected_rows = set(self._selected_visible_rows())
        if row not in selected_rows:
            self.table.selectRow(row)
        plan = self.plans[row]
        path = plan.analysis.item.path
        menu = QMenu(self)

        explorer_action = menu.addAction(tr("Datei im Explorer anzeigen"))
        explorer_action.triggered.connect(lambda: self._open_file_location(path))
        open_action = menu.addAction(tr("Datei öffnen"))
        open_action.triggered.connect(lambda: self._open_file(path))
        remove_action = menu.addAction(tr("Aus Tabelle entfernen"))
        remove_action.triggered.connect(self.remove_selected_rows_from_table)

        diff_actions: list[tuple[str, object]] = []
        media_diff = self._matching_media_diff_template(plan)
        if media_diff is not None:
            label, template_kind = media_diff
            diff_actions.append((label, lambda checked=False, kind=template_kind: self._open_diff_for_row(row, kind)))
        text_label = self._text_diff_label_for_plan(plan)
        if text_label is not None:
            diff_actions.append((text_label, lambda: self._open_exif_text_diff_for_row(row)))
        if diff_actions:
            menu.addSeparator()
            for label, callback in diff_actions:
                action = menu.addAction(label)
                action.triggered.connect(callback)

        menu.exec(self.table.viewport().mapToGlobal(position))

    def update_selection(self) -> None:
        """Update preview and details for the selected row."""
        rows = self._selected_visible_rows()
        if not rows:
            return
        row = rows[0]
        if row < 0 or row >= len(self.plans):
            return
        plan = self.plans[row]
        # Show the action/details immediately; the image decodes in the background.
        bucket = video_bucket_label_text(plan.analysis, self.settings_model)
        self.details.setMarkdown(details_markdown(plan, bucket))
        self._preview_controller.request(plan)

    def _open_file_location(self, path: Path) -> None:
        """Open the containing folder and select the file when possible."""
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", str(path)])  # noqa: S603
            else:
                subprocess.Popen(["xdg-open", str(path.parent)])  # noqa: S603
        except OSError as exc:
            QMessageBox.warning(self, "Explorer", tr("Konnte Ordner nicht öffnen:\n{path}\n\n{error}").format(path=path, error=exc))

    def _open_file(self, path: Path) -> None:
        """Open one media file with the default OS application."""
        LOGGER.info("Opening file via OS default app: %s", path)
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(path)])  # noqa: S603
        except OSError as exc:
            LOGGER.error("Could not open file '%s': %s", path, exc)
            QMessageBox.warning(
                self,
                tr("Datei öffnen"),
                tr("Konnte Datei nicht öffnen:\n{path}\n\n{error}").format(path=path, error=exc),
            )

    def open_active_item_location(self) -> None:
        """Open the folder of the selected or first available media item."""
        path = self._active_item_path()
        if path is None:
            QMessageBox.information(self, "Explorer", tr("Keine aktive Datei ausgewählt."))
            return
        self._open_file_location(path)

    def _active_item_path(self) -> Path | None:
        """Return the selected row path, or the first plan when nothing is selected."""
        rows = self._selected_visible_rows()
        if rows:
            row = rows[0]
            if 0 <= row < len(self.plans):
                return self.plans[row].analysis.item.path
        if self.plans:
            return self.plans[0].analysis.item.path
        return None

    def _rebuild_row_map(self) -> None:
        """Map current plan paths, and known original paths, to table rows."""
        self._row_by_path.clear()
        for row, plan in enumerate(self.plans):
            self._row_by_path[path_key(plan.analysis.item.path)] = row
            original = self._applied_plan_sources.get(id(plan))
            if original is not None:
                self._row_by_path[path_key(original)] = row

    def _row_for_path(self, path: Path) -> int | None:
        """Return the row for a current or original media path."""
        row = self._row_by_path.get(path_key(path))
        if row is not None and 0 <= row < len(self.plans):
            return row
        for index, plan in enumerate(self.plans):
            if same_path(plan.analysis.item.path, path):
                self._row_by_path[path_key(path)] = index
                return index
            original = self._applied_plan_sources.get(id(plan))
            if original is not None and same_path(original, path):
                self._row_by_path[path_key(path)] = index
                return index
        return None

    def _active_actionable_plans(self) -> list[MediaPlan]:
        """Return active plans that can still participate in pipeline operations."""
        return [plan for plan in self.plans if plan.analysis.status != PlanStatus.SKIP and plan.final_path is not None]

    def _refresh_active_buttons(self) -> None:
        """Refresh action buttons from the active table-backed pipeline."""
        actionable_plans = self._active_actionable_plans()
        self.run_button.setEnabled(bool(actionable_plans))
        self.run_selected_button.setEnabled(False)
        self.run_images_button.setEnabled(any(plan.analysis.item.kind == MediaKind.IMAGE for plan in actionable_plans))
        self.run_videos_button.setEnabled(any(plan.analysis.item.kind == MediaKind.VIDEO for plan in actionable_plans))
        self.export_button.setEnabled(bool(self.plans))
        self.missing_button.setEnabled(bool(self.results))
        self.lasso_button.setEnabled(bool(self.results))

    def _remove_active_paths(self, paths: list[Path], reason: str = "") -> int:
        """Remove paths from the active table-backed pipeline without deleting files."""
        remove_keys = {path_key(path) for path in paths}
        if not remove_keys:
            return 0
        keep_results: list[AnalysisResult] = []
        keep_plans: list[MediaPlan] = []
        removed_plans: list[MediaPlan] = []
        for result, plan in zip(self.results, self.plans):
            if path_key(result.item.path) in remove_keys:
                removed_plans.append(plan)
                continue
            keep_results.append(result)
            keep_plans.append(plan)
        if not removed_plans:
            return 0
        for plan in removed_plans:
            self._applied_plan_sources.pop(id(plan), None)
        self.results = keep_results
        self.plans = keep_plans
        self.table.clearSelection()
        self._preview_controller.clear()
        self.details.clear()
        self.populate_table()
        self._refresh_active_buttons()
        suffix = f" ({reason})" if reason else ""
        self.status_label.setText(
            tr("{count} Medien aus Tabelle entfernt{suffix}. {remaining} verbleibend.").format(
                count=len(removed_plans), suffix=suffix, remaining=len(self.results)
            )
        )
        return len(removed_plans)

    def remove_selected_rows_from_table(self) -> None:
        """Remove selected rows from the active pipeline list, not from disk."""
        rows = self._selected_visible_rows()
        if not rows:
            return
        paths = [self.plans[row].analysis.item.path for row in rows if 0 <= row < len(self.plans)]
        if not paths:
            return
        if len(paths) > 1:
            confirm = QMessageBox.question(
                self,
                tr("Aus Tabelle entfernen"),
                tr(
                    "{count} Medien nur aus der aktuellen Tabelle/Pipeline entfernen?\n\n"
                    "Die Dateien bleiben auf der Festplatte."
                ).format(count=len(paths)),
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
        self._remove_active_paths(paths, tr("manuell"))

