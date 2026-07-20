"""Folder opening, drag&drop, and scan orchestration for the main window (mixin)."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import QFileDialog, QMessageBox

from ...core.discovery import discover_media, normalize_root
from ...core.i18n import tr
from ..workers import ScanWorker
from ...core.logging_config import get_logger
from ...core.models import AnalysisResult, MediaItem, MediaKind, MediaPlan
from ...reports import vacation_span_warning

LOGGER = get_logger(__name__)


class ScanFlowMixin:
    """Folder open/drop entry points and scan worker lifecycle for MainWindow."""

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        """Accept folder drops."""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        """Open all dropped folders according to the persistent drop behavior."""
        folders = [Path(url.toLocalFile()) for url in event.mimeData().urls()]
        folders = [path for path in folders if path.is_dir()]
        if not folders:
            QMessageBox.warning(self, "Drop folder", tr("Bitte einen Ordner droppen."))
            return
        self._handle_dropped_folders(folders)

    def _open_multiple_folders(self, folders: list[Path]) -> None:
        """Compatibility wrapper for opening a dropped folder batch."""
        self._handle_dropped_folders(folders)

    def _scan_is_running(self) -> bool:
        """Return whether the occupied worker slot currently belongs to a scan."""
        return isinstance(self.worker, ScanWorker) and bool(
            self.worker_thread and self.worker_thread.isRunning()
        )

    def _ask_drop_action(self, folders: list[Path]) -> str | None:
        """Ask once whether a complete dropped batch should add or replace."""
        folder_lines = "\n".join(f"• {folder}" for folder in folders)
        answer = QMessageBox.question(
            self,
            tr("Ordner hinzufügen"),
            tr(
                "Es ist bereits eine Dateiliste geöffnet.\n\n"
                "Neu gedroppte Ordner ({count}):\n{folders}\n\n"
                "Ja = Vorhandene Liste ersetzen\n"
                "Nein = Dateien zusätzlich hinzufügen"
            ).format(count=len(folders), folders=folder_lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return None
        return "replace" if answer == QMessageBox.StandardButton.Yes else "add"

    def _handle_dropped_folders(self, folders: list[Path]) -> None:
        """Validate and schedule every folder from one drag-and-drop operation."""
        normalized_folders: list[Path] = []
        for folder in folders:
            try:
                normalized = normalize_root(folder)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Dropped folder could not be opened, skipping %s: %s", folder, exc)
                continue
            if normalized not in normalized_folders:
                normalized_folders.append(normalized)
        if not normalized_folders:
            QMessageBox.warning(self, "Drop folder", tr("Bitte einen Ordner droppen."))
            return

        scan_running = self._scan_is_running()
        if self._has_running_work() and not scan_running:
            self._block_if_busy()
            return

        action = "replace"
        if self.roots:
            action = self.settings_model.folder_drop_behavior
            if action == "ask":
                action = self._ask_drop_action(normalized_folders)
                if action is None:
                    LOGGER.info("Folder drop cancelled by user")
                    return

        if not scan_running:
            LOGGER.info(
                "Starting one scan for %s dropped folder(s), action=%s",
                len(normalized_folders), action,
            )
            self._start_dropped_folder_batch(normalized_folders, replace_existing=action == "replace")
            return

        # Drops made after a worker has already started retain the existing safe
        # sequential queue. Only one simultaneous multi-drop becomes one scan.
        if action == "replace":
            self._pending_folders.clear()
        self._pending_folders.extend(
            (folder, action == "replace" and index == 0)
            for index, folder in enumerate(normalized_folders)
        )
        LOGGER.info("Queued %s dropped folder(s) behind the running scan", len(normalized_folders))
        self.status_label.setText(
            tr("{count} Ordner zum laufenden Scan vorgemerkt.").format(count=len(normalized_folders))
        )

    def _start_dropped_folder_batch(self, folders: list[Path], replace_existing: bool) -> None:
        """Load one dropped folder batch and scan all of its files together."""
        if replace_existing:
            self._pending_folders.clear()
            self._reset_for_new_root(folders[0])
            self.roots.extend(folders[1:])
            roots_to_scan = folders
        else:
            roots_to_scan = [folder for folder in folders if folder not in self.roots]
            if not roots_to_scan:
                QMessageBox.information(self, tr("Ordner"), tr("Dieser Ordner ist bereits geöffnet."))
                return
            self.roots.extend(roots_to_scan)
            self._scan_merge = True
        self.folder_label.setText(" + ".join(str(root) for root in self.roots))
        self._discover_and_scan_roots(roots_to_scan)

    def choose_folder(self) -> None:
        """Show a folder picker."""
        LOGGER.info("Folder picker opened")
        selected = QFileDialog.getExistingDirectory(self, tr("Medienordner öffnen"))
        if selected:
            LOGGER.info("Folder picker selected %s", selected)
            self.open_folder(Path(selected))
        else:
            LOGGER.info("Folder picker cancelled")

    def open_folder(self, path: Path) -> None:
        """Open a folder — ask ADD or REPLACE when a folder is already open."""
        if self._block_if_busy():
            return
        try:
            normalized = normalize_root(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("Ordner öffnen"), str(exc))
            return
        if self.roots:
            answer = QMessageBox.question(
                self,
                tr("Ordner hinzufügen"),
                tr(
                    "Es ist bereits ein Ordner geöffnet.\n\n"
                    "Neu: {folder}\n\n"
                    "Ja = Nur diesen neuen Ordner verwenden (ersetzen)\n"
                    "Nein = Dateien zusätzlich hinzufügen (additiv)"
                ).format(folder=normalized),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                LOGGER.info("Folder open cancelled by user")
                return
            if answer == QMessageBox.StandardButton.Yes:
                self._reset_for_new_root(normalized)
            else:
                self._add_folder(normalized)
                return
        else:
            self._reset_for_new_root(normalized)
        self._discover_and_scan(normalized)

    def _reset_for_new_root(self, normalized: Path) -> None:
        """Reset all state for a single new root folder."""
        self.roots = [normalized]
        self._scan_merge = False
        self.folder_label.setText(str(normalized))
        self.results = []
        self.plans = []
        self._row_by_path.clear()
        self.table.setRowCount(0)
        self.details.clear()
        self._preview_controller.clear()
        self.run_button.setEnabled(False)
        self.run_images_button.setEnabled(False)
        self.run_videos_button.setEnabled(False)
        self.run_selected_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.missing_button.setEnabled(False)
        self.lasso_button.setEnabled(False)
        self._update_missing_exif_badge()
        self._update_pairs_badge()
        self.jpeg_fix_button.setEnabled(True)
        self.status_label.setText(tr("Ordner geöffnet. Dateiliste wird aufgebaut..."))
        LOGGER.info("Reset for new root %s", normalized)

    def _add_folder(self, normalized: Path) -> None:
        """Add a folder additively to the existing set of roots."""
        if normalized in self.roots:
            LOGGER.info("Folder already added, skipping: %s", normalized)
            QMessageBox.information(self, tr("Ordner"), tr("Dieser Ordner ist bereits geöffnet."))
            return
        self.roots.append(normalized)
        self._scan_merge = True
        self.folder_label.setText(" + ".join(str(r) for r in self.roots))
        self.status_label.setText(tr("Ordner hinzugefügt: {folder}. Scan läuft...").format(folder=normalized))
        LOGGER.info("Adding folder additively: %s (roots now: %s)", normalized, self.roots)
        self._discover_and_scan(normalized)

    def _discover_and_scan(self, normalized: Path) -> None:
        """Discover media in a folder and start a scan."""
        self._discover_and_scan_roots([normalized])

    def _discover_and_scan_roots(self, roots: list[Path]) -> None:
        """Discover all roots first, show one pending list, then start one scan."""
        self._sync_settings_from_ui()
        items: list[MediaItem] = []
        try:
            for root in roots:
                items.extend(discover_media(root, recursive=self.settings_model.recursive))
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Could not discover media for folder batch: %s", roots)
            QMessageBox.critical(self, tr("Dateiliste"), str(exc))
            return
        existing_paths = (
            {plan.analysis.item.path.resolve() for plan in self.plans}
            if self._scan_merge
            else set()
        )
        new_items: list[MediaItem] = []
        seen_paths = set(existing_paths)
        for item in items:
            resolved_path = item.path.resolve()
            if resolved_path in seen_paths:
                continue
            seen_paths.add(resolved_path)
            new_items.append(item)
        if self._scan_merge:
            self.plans.extend(self._pending_plan(item) for item in new_items)
            LOGGER.info("Additive discovery: %s new items (skipped %s duplicates)", len(new_items), len(items) - len(new_items))
        else:
            self.plans = [self._pending_plan(item) for item in new_items]
        self.populate_table()
        self.status_label.setText(tr("{count} verarbeitbare Datei(en). Scan startet...").format(count=len(self.plans)))
        LOGGER.info(
            "Combined file list contains %s processable files from %s folder(s)",
            len(new_items), len(roots),
        )
        self.scan(roots[0], items=new_items)

    def scan(self, root: Path | None = None, items: list[MediaItem] | None = None) -> None:
        """Start scanning a discovered item batch, or discover the selected root."""
        scan_root = root or self.root
        if scan_root is None:
            LOGGER.warning("Scan requested without root")
            QMessageBox.warning(self, tr("Kein Ordner"), tr("Bitte zuerst einen Ordner öffnen."))
            return
        if self._block_if_busy():
            return
        self._sync_settings_from_ui()
        self._set_busy(True)
        self.progress.setValue(0)
        self.status_label.setText(tr("Scan läuft..."))
        LOGGER.info("Starting scan worker for %s", scan_root)
        worker = ScanWorker(scan_root, self.settings_model, items=items)
        worker.partial.connect(self.on_scan_partial)
        self._start_worker(worker, self.on_scan_finished)

    def on_scan_partial(self, results: list[AnalysisResult], plans: list[MediaPlan]) -> None:
        """Render partial scan results while the worker continues."""
        LOGGER.info("Scan partial received results=%s plans=%s existing_plans=%s", len(results), len(plans), len(self.plans))
        if self._scan_merge:
            new_paths = {r.item.path.resolve() for r in results}
            self.results = [r for r in self.results if r.item.path.resolve() not in new_paths]
            self.results.extend(results)
            plan_map = {p.analysis.item.path.resolve(): p for p in plans}
            self.plans = [p for p in self.plans if p.analysis.item.path.resolve() not in plan_map]
            self.plans.extend(plans)
        else:
            self.results = results
            if self.plans and len(self.plans) >= len(plans):
                merged = self.plans.copy()
                merged[: len(plans)] = plans
                self.plans = merged
            else:
                self.plans = plans
        self.populate_table()

    def _process_pending_folders(self) -> None:
        """Start the scan for the next queued dropped folder, if any.

        Called from ``_clear_worker_refs`` (i.e. after the previous scan's
        QThread has fully finished), so ``_block_if_busy`` cannot veto it.
        """
        while self._pending_folders:
            next_folder, replace_existing = self._pending_folders.pop(0)
            try:
                next_normalized = normalize_root(next_folder)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Queued folder could not be opened, skipping %s: %s", next_folder, exc)
                continue
            LOGGER.info(
                "Processing queued dropped folder: %s (replace=%s)",
                next_normalized, replace_existing,
            )
            if replace_existing:
                self._reset_for_new_root(next_normalized)
                self._discover_and_scan(next_normalized)
                return
            if next_normalized in self.roots:
                LOGGER.info("Queued folder already added, skipping: %s", next_normalized)
                continue
            self._add_folder(next_normalized)
            return

    def on_scan_finished(self, results: list[AnalysisResult], plans: list[MediaPlan]) -> None:
        """Render scan results, merging additively when in merge mode."""
        if self._scan_merge:
            existing_paths = {r.item.path.resolve() for r in self.results}
            new_result_paths = set()
            for result in results:
                resolved_path = result.item.path.resolve()
                new_result_paths.add(resolved_path)
                if resolved_path not in existing_paths:
                    self.results.append(result)
                    existing_paths.add(resolved_path)
            merged_plans = [p for p in self.plans if p.analysis.item.path.resolve() not in new_result_paths]
            merged_plans.extend(plans)
            self.plans = merged_plans
            self._scan_merge = False
            LOGGER.info("Scan merged: total results=%s plans=%s", len(self.results), len(self.plans))
        else:
            self.results = results
            self.plans = plans
        all_plans = self.plans
        LOGGER.info("Scan finished in GUI results=%s plans=%s actionable=%s", len(self.results), len(all_plans), sum(1 for plan in all_plans if plan.actions))
        self.populate_table()
        if self._pending_folders:
            # Keep Apply/open controls disabled across the tiny gap between the
            # current QThread finishing and the next queued scan starting.
            self._update_missing_exif_badge()
            self._update_pairs_badge()
            self.status_label.setText(
                tr("Ordnerscan abgeschlossen. Noch {count} Ordner in der Warteschlange.").format(
                    count=len(self._pending_folders)
                )
            )
            return
        self._set_busy(False)
        actionable_plans = [plan for plan in all_plans if plan.actions]
        self.run_button.setEnabled(bool(actionable_plans))
        self.run_images_button.setEnabled(any(plan.analysis.item.kind == MediaKind.IMAGE for plan in actionable_plans))
        self.run_videos_button.setEnabled(any(plan.analysis.item.kind == MediaKind.VIDEO for plan in actionable_plans))
        self.run_selected_button.setEnabled(bool(actionable_plans))
        self.export_button.setEnabled(bool(actionable_plans))
        self.missing_button.setEnabled(bool(actionable_plans))
        self.lasso_button.setEnabled(bool(self.results))
        self._update_missing_exif_badge()
        self._update_pairs_badge()
        self.status_label.setText(tr("Plan bereit: {count} Dateien.").format(count=len(self.results)))
        span_warning = vacation_span_warning(
            [plan.analysis for plan in all_plans],
            self.settings_model.metadata.vacation_span_weeks,
        )
        if span_warning is not None:
            LOGGER.info("Vacation span warning shown after scan: %s", span_warning)
            QMessageBox.information(
                self,
                tr("Aufnahmezeitraum"),
                span_warning,
            )

