"""Main PyQt6 application window."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSlot
from PyQt6.QtGui import QCloseEvent, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QMessageBox,
)

from .apply_flow import ApplyFlowMixin
from .diff_actions import DiffActionsMixin
from ..common.dialogs import show_missing_exif_dialog
from .layout import build_log_dock, build_ui
from .scan_flow import ScanFlowMixin
from .status_stats import StatusStatsMixin
from .table_actions import TableActionsMixin
from .worker_lifecycle import WorkerLifecycleMixin
from .overlay_flow import OverlayFlowMixin
from .window_geometry import WindowGeometryMixin
from .workflow_panel import WorkflowSettingsMixin
from ..common.theme import (
    FastTooltipStyle,
    app_icon_path,
    asset_path,
    main_window_stylesheet,
)
from ..common.widgets import (
    LogRelay,
    distribute_column_widths,  # noqa: F401  (re-exported for tests/back-compat)
)

from ..settings_dialog import SettingsDialog
from ...core.processes import (
    launch_gui_tool,
    resolve_executable,
)
from ...core.logging_config import configure_logging, get_logger, log_path, log_to_file, setup_gui_logging
from ...core.models import (
    AnalysisResult,
    AppSettings,
    ApplyMode,
    Confidence,
    MediaItem,
    MediaKind,
    MediaPlan,
    PlanStatus,
    RawMetadata,
    ResolvedTimestamp,
)
from ...reports import export_excel_report, missing_exif_rows
from ...metadata import analyze_item
from ...planner import build_plans
from ...core.i18n import init_language, tr
from ...core.settings import load_settings, save_settings

LOGGER = get_logger(__name__)


def _missing_pipeline_tools(settings: AppSettings) -> list[tuple[str, str]]:
    """Return required pipeline tools that could not be resolved."""
    checks = [
        ("ExifTool", settings.tools.exiftool),
        ("FFmpeg", settings.tools.ffmpeg),
        ("FFprobe", settings.tools.ffprobe),
        ("NConvert", settings.tools.xnconvert),
    ]
    missing: list[tuple[str, str]] = []
    for display_name, configured in checks:
        if resolve_executable(configured) is None:
            missing.append((display_name, configured))
    return missing


class MainWindow(
    ScanFlowMixin,
    ApplyFlowMixin,
    DiffActionsMixin,
    OverlayFlowMixin,
    TableActionsMixin,
    StatusStatsMixin,
    WorkerLifecycleMixin,
    WorkflowSettingsMixin,
    WindowGeometryMixin,
    QMainWindow,
):
    """Main window for the Vacation Media Processor GUI."""

    def __init__(self) -> None:
        super().__init__()
        self.settings_model = load_settings()
        self.roots: list[Path] = []
        self._scan_merge: bool = False
        # (folder, replace_existing): queued folder scans are always sequential.
        self._pending_folders: list[tuple[Path, bool]] = []
        self._original_sizes: dict[Path, int] = {}
        self._backup_paths: dict[Path, Path] = {}
        self._applied_plan_roots: dict[int, Path] = {}
        self._applied_plan_sources: dict[int, Path] = {}
        self._row_by_path: dict[str, int] = {}
        self._last_apply_run_id: str | None = None
        self._readback_diff_paths: tuple[Path, Path] | None = None
        self.results: list[AnalysisResult] = []
        self.plans: list[MediaPlan] = []
        self._applied_plans: list[MediaPlan] = []
        self._analysis_refresh_pending = False
        self._workflow_refresh_timer = QTimer(self)
        self._workflow_refresh_timer.setSingleShot(True)
        self._workflow_refresh_timer.setInterval(250)
        self._workflow_refresh_timer.timeout.connect(self._apply_workflow_refresh)
        # Stats need a stat() call per file; debounce so rapid per-item apply
        # updates cannot trigger a full disk sweep for every finished file.
        self._stats_timer = QTimer(self)
        self._stats_timer.setSingleShot(True)
        self._stats_timer.setInterval(300)
        self._stats_timer.timeout.connect(self._update_stats)
        self.worker_thread: QThread | None = None
        self.worker: object | None = None
        self.log_file = configure_logging()
        LOGGER.info("GUI started. Logfile: %s", self.log_file)
        self.setWindowTitle("Vacation Media Processor")
        icon_path = app_icon_path()
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.setMinimumSize(640, 480)
        if not self._restore_window_geometry():
            width, height, position = self._initial_window_geometry()
            self.resize(width, height)
            if position is not None:
                self.move(position)
        self.setAcceptDrops(True)
        build_ui(self)
        self._apply_style()
        # Thread-safe log relay: a separate QObject carries the signal
        # so PyQt6 properly queues cross-thread emissions from workers.
        self._log_relay = LogRelay()
        self._log_relay.message.connect(self._append_log_impl)
        build_log_dock(self)
        setup_gui_logging(self._log_relay.message.emit)
        self._check_tool_status()

    @property
    def root(self) -> Path | None:
        """Return the primary (first) root, or None when no folder is open."""
        return self.roots[0] if self.roots else None

    def closeEvent(self, event: QCloseEvent) -> None:
        """Persist window geometry before closing."""
        if self._has_running_work():
            answer = QMessageBox.question(
                self,
                tr("Laufender Prozess"),
                tr(
                    "Es läuft gerade noch eine Verarbeitung.\n\n"
                    "Soll der laufende Prozess wirklich abgebrochen und das Programm geschlossen werden?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._abort_running_work()
        self._sync_settings_from_ui()
        self._remember_window_geometry()
        save_settings(self.settings_model)
        super().closeEvent(event)

    def populate_table(self) -> None:
        """Fill the media table from plans."""
        self._table_controller.populate()

    def _refresh_table_row(self, row: int) -> None:
        """Refresh visible cells for one plan row."""
        self._table_controller.refresh_row(row)

    def _refresh_workflow_table_columns(self) -> None:
        """Refresh only table cells affected by workflow setting changes."""
        self._table_controller.refresh_workflow_columns()

    def _apply_table_filters(self) -> None:
        """Hide rows that do not match the active per-column filters."""
        self._table_controller.apply_filters()

    def _selected_visible_rows(self) -> list[int]:
        """Return selected table rows that are currently visible after filtering."""
        return self._table_controller.selected_visible_rows()

    def _apply_table_font(self) -> None:
        """Apply the configured font size to the table."""
        self._table_controller.apply_font_size(self.settings_model.table_font_size)

    def _apply_table_font_size(self, size: int) -> None:
        """Apply a specific font size to the table (live preview from settings)."""
        self._table_controller.apply_font_size(size)

    def _fit_table_columns(self) -> None:
        """Re-fit the table columns to the current viewport width."""
        controller = getattr(self, "_table_controller", None)
        if controller is not None:
            controller.fit_columns()

    def _fit_side_panel(self) -> None:
        """Size the sidebar exactly wide enough that no control is clipped.

        Must run after the stylesheet and fonts are applied (e.g. on first show),
        because spinbox/combobox padding from the stylesheet widens the controls
        and a build-time estimate would be too small.
        """
        panel = getattr(self, "_side_scroll", None)
        if panel is None:
            return
        content = panel.widget()
        if content is None:
            return
        content.adjustSize()
        needed = content.sizeHint().width()
        scrollbar = panel.verticalScrollBar().sizeHint().width()
        fit_width = max(320, needed + scrollbar + panel.frameWidth() * 2 + 8)
        self._side_panel_fit_width = fit_width
        panel.setMinimumWidth(fit_width)
        splitter = getattr(self, "_main_splitter", None)
        if splitter is not None and splitter.width() > 0:
            total = splitter.width()
            left = max(400, total - fit_width)
            splitter.setSizes([left, fit_width])

    def showEvent(self, event) -> None:  # type: ignore[override]
        """Refit the sidebar and table columns once the window is shown."""
        super().showEvent(event)
        if not getattr(self, "_side_fitted", False):
            self._side_fitted = True
            self._fit_side_panel()
            QTimer.singleShot(0, self._fit_side_panel)
        self._fit_table_columns()
        QTimer.singleShot(0, self._fit_table_columns)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        """Re-fit the table columns to the new window width."""
        super().resizeEvent(event)
        self._fit_table_columns()

    def _asset_icon(self, name: str) -> QIcon:
        """Load an SVG icon from the bundled assets directory."""
        return QIcon(asset_path(name).as_posix())

    def _refresh_plans_from_results(self) -> None:
        """Rebuild the current plans from the latest results and active settings."""
        if not self.results:
            self._update_missing_exif_badge()
            self._update_pairs_badge()
            return
        selected_row = self.table.currentRow()
        new_plans = build_plans(self.results, self.settings_model)
        if self.table.rowCount() != len(new_plans):
            self.plans = new_plans
            self.populate_table()
        else:
            self.plans = new_plans
            self._refresh_workflow_table_columns()
        if 0 <= selected_row < len(self.plans):
            self.table.selectRow(selected_row)
            self.update_selection()
        actionable_plans = [plan for plan in self.plans if plan.actions]
        self.run_button.setEnabled(bool(actionable_plans))
        self.run_images_button.setEnabled(any(plan.analysis.item.kind == MediaKind.IMAGE for plan in actionable_plans))
        self.run_videos_button.setEnabled(any(plan.analysis.item.kind == MediaKind.VIDEO for plan in actionable_plans))
        self.run_selected_button.setEnabled(bool(actionable_plans))
        self.export_button.setEnabled(bool(actionable_plans))
        self.missing_button.setEnabled(bool(actionable_plans))
        self._update_missing_exif_badge()
        self._update_pairs_badge()

    def _reanalyze_results_for_current_settings(self) -> None:
        """Re-evaluate timestamp resolution after live analysis-setting changes."""
        refreshed_results: list[AnalysisResult] = []
        for previous in self.results:
            if previous.status == PlanStatus.DONE or (
                previous.status == PlanStatus.SKIP
                and "ExifTool returned no metadata." in previous.warnings
            ):
                refreshed_results.append(previous)
                continue
            refreshed = analyze_item(previous.item, previous.metadata, self.settings_model.metadata)
            # FFprobe may have enriched these fields after the ExifTool analysis;
            # a pure timestamp re-analysis must not discard that information.
            refreshed.width = refreshed.width if refreshed.width is not None else previous.width
            refreshed.height = refreshed.height if refreshed.height is not None else previous.height
            refreshed.codec = refreshed.codec if refreshed.codec is not None else previous.codec
            refreshed.fps = refreshed.fps if refreshed.fps is not None else previous.fps
            refreshed.has_depth = refreshed.has_depth or previous.has_depth
            refreshed_results.append(refreshed)
        self.results = refreshed_results

    def _apply_style(self) -> None:
        """Apply the application stylesheet."""
        self.setStyleSheet(main_window_stylesheet())


    # ── GUI Log Panel ──────────────────────────────────────────────────

    def _on_log_dock_visibility(self, visible: bool) -> None:
        """Sync the Log-Panel menu toggle with dock visibility."""
        self._toggle_log_action.setChecked(visible)

    def _toggle_log_dock(self) -> None:
        """Toggle the log dock panel."""
        self.log_dock.setVisible(not self.log_dock.isVisible())

    def _check_tool_status(self) -> None:
        """Check required external tools and warn if missing."""
        missing = _missing_pipeline_tools(self.settings_model)
        if not missing:
            self.status_label.setText(
                tr("Bereit. Pipeline-Tools gefunden – Ordner öffnen und loslegen.")
            )
            return

        body_lines = [tr("Diese Pipeline-Tools wurden nicht gefunden:"), ""]
        body_lines.extend(f"- {display_name}: {configured}" for display_name, configured in missing)
        body_lines.extend([
            "",
            tr("Bitte installieren, in PATH legen oder unter Settings > Settings öffnen ... konfigurieren."),
        ])
        message = "\n".join(body_lines)
        missing_names = ", ".join(display_name for display_name, _configured in missing)
        self.status_label.setText(tr("⚠️ Pipeline-Tools fehlen: {missing_names}.").format(missing_names=missing_names))
        LOGGER.warning(
            "Missing pipeline tools at startup: %s",
            "; ".join(f"{display_name}={configured}" for display_name, configured in missing),
        )
        QMessageBox.warning(
            self,
            tr("Pipeline Tools fehlen"),
            message,
        )

    @pyqtSlot(str)
    def _append_log_impl(self, message: str) -> None:
        """Slot running on the GUI thread — actually appends to widget."""
        try:
            if self.log_text is None:
                return
            self.log_text.appendPlainText(message)
        except Exception:
            log_to_file("_append_log_impl crashed, GUI log panel unavailable")

    def launch_configured_tool(self, executable: str, display_name: str, pass_root: bool) -> None:
        """Launch a configured GUI tool without blocking the app."""
        executable = executable.strip()
        LOGGER.info(
            "Launch requested display_name=%s executable=%s pass_root=%s root=%s",
            display_name, executable, pass_root, self.root,
        )
        if not executable:
            LOGGER.warning("Optional helper not configured: %s", display_name)
            QMessageBox.warning(
                self,
                display_name,
                tr("{display_name} wurde nicht gefunden. Bitte in Settings konfigurieren oder installieren.").format(display_name=display_name),
            )
            return

        resolved = resolve_executable(executable)
        if resolved is None:
            LOGGER.warning("Optional helper not resolvable: %s executable=%s", display_name, executable)
            QMessageBox.warning(
                self,
                display_name,
                tr("{display_name} wurde nicht gefunden. Bitte in Settings konfigurieren oder installieren.").format(display_name=display_name),
            )
            return

        extra_args = [str(self.root)] if pass_root and self.root is not None else []
        try:
            process = launch_gui_tool(resolved, extra_args)
            LOGGER.info("Launch handed to OS for %s pid=%s", display_name, process.pid)
        except OSError as exc:
            LOGGER.exception("%s could not be launched: %s", display_name, exc)
            QMessageBox.critical(
                self, display_name,
                tr("{display_name} konnte nicht gestartet werden:\n{error}").format(display_name=display_name, error=exc),
            )

    def open_logfile(self) -> None:
        """Open the active logfile in the default editor."""
        path = configure_logging() or log_path()
        try:
            path.touch(exist_ok=True)
            os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
        except OSError as exc:
            QMessageBox.critical(self, "Logfile", tr("Logfile konnte nicht geöffnet werden:\n{path}\n\n{error}").format(path=path, error=exc))

    def open_settings_dialog(self) -> None:
        """Open the separate tool-path settings dialog."""
        LOGGER.info("Opening settings dialog")
        from PyQt6.QtCore import QLocale

        from ...core.i18n import current_language, resolve_language

        dialog = SettingsDialog(self.settings_model, self, root=self.root)
        dialog.exec()
        self._apply_table_font()
        new_language = resolve_language(self.settings_model.language, QLocale.system().name())
        if new_language != current_language():
            self._switch_language(new_language)

    # Session state that survives a live language switch (window rebuild).
    _SESSION_ATTRS = (
        "roots",
        "results",
        "plans",
        "_original_sizes",
        "_backup_paths",
        "_applied_plans",
        "_applied_plan_sources",
        "_applied_plan_roots",
        "_last_apply_run_id",
        "_scan_merge",
        "_pending_folders",
    )

    def _switch_language(self, new_language: str) -> None:
        """Apply a language change live by rebuilding the window with the session state.

        Every user-visible string goes through ``tr()`` at widget-build time, so
        a fresh window in the new language is equivalent to full retranslation —
        without retranslate plumbing in every module. The scan session (folders,
        results, plans, backup maps) is carried over.
        """
        if self._has_running_work():
            QMessageBox.information(
                self,
                tr("Sprache"),
                tr("Der Sprachwechsel wird übernommen, sobald die laufende Verarbeitung abgeschlossen und die Anwendung neu gestartet ist."),
            )
            return
        LOGGER.info("Switching UI language live to %s (window rebuild)", new_language)
        init_language(new_language)
        replacement = MainWindow()
        for name in self._SESSION_ATTRS:
            setattr(replacement, name, getattr(self, name))
        if replacement.roots:
            replacement.folder_label.setText(" + ".join(str(root) for root in replacement.roots))
        replacement._set_readback_diff_paths(self._readback_diff_paths)
        if replacement.results:
            # Rebuild plans from the existing scan results (pure CPU, no rescan)
            # so the planner-generated action texts translate immediately too —
            # the same refresh every workflow-settings change already performs.
            replacement._refresh_plans_from_results()
        else:
            replacement.populate_table()
        replacement._set_busy(False)
        if replacement.results:
            replacement.status_label.setText(tr("Plan bereit: {count} Dateien.").format(count=len(replacement.results)))
        replacement.restoreGeometry(self.saveGeometry())
        # Keep the new window referenced beyond this method (run_app holds only
        # the first window); the application object outlives both.
        QApplication.instance()._vmp_window = replacement  # type: ignore[attr-defined]
        replacement.show()
        self.close()
        self.deleteLater()

    def save_current_settings(self) -> None:
        """Persist current UI settings."""
        self._sync_settings_from_ui()
        self._remember_window_geometry()
        save_settings(self.settings_model)
        QMessageBox.information(self, "Settings", tr("Settings gespeichert."))

    def _sync_settings_from_ui(self) -> None:
        """Copy UI control values into the settings model."""
        self.settings_model.recursive = self.recursive_check.isChecked()
        self.settings_model.skip_backup = self.skip_backup_check.isChecked()
        self.settings_model.read_after_exif = self.read_after_exif_check.isChecked()
        self.settings_model.images.jpeg_quality = self.quality_spin.value()
        self.settings_model.images.heic_heif_to_jpg = self.heic_heif_convert_check.isChecked()
        self.settings_model.images.heic_heif_jpeg_quality = self.heic_heif_quality_spin.value()
        self.settings_model.images.skip_depth_heic_conversion = self.skip_depth_heic_check.isChecked()
        self.settings_model.images.preserve_depth_as_gdepth = self.preserve_depth_gdepth_check.isChecked()
        self.settings_model.images.png_to_jpg = self.png_convert_check.isChecked()
        self.settings_model.images.jpeg_rotate_by_exif = self.jpeg_rotate_check.isChecked()
        self.settings_model.images.jpeg_rebuild_exif_thumbnail = self.jpeg_thumb_check.isChecked()
        self.settings_model.videos.fhd_crf = self.fhd_spin.value()
        self.settings_model.videos.qhd_crf = self.qhd_spin.value()
        self.settings_model.videos.uhd_crf = self.uhd_spin.value()
        self.settings_model.videos.limit_to_fhd = self.limit_fhd_check.isChecked()
        self.settings_model.videos.limit_to_30_fps = self.limit_fps_check.isChecked()
        qhd_threshold = self.qhd_threshold_spin.value()
        uhd_threshold = self.uhd_threshold_spin.value()
        if uhd_threshold <= qhd_threshold:
            uhd_threshold = qhd_threshold + 1
            self.uhd_threshold_spin.setValue(uhd_threshold)
        self.settings_model.videos.qhd_long_edge_threshold = qhd_threshold
        self.settings_model.videos.uhd_long_edge_threshold = uhd_threshold
        self.settings_model.videos.audio_codec = str(self.audio_codec_combo.currentData())
        self.settings_model.videos.audio_bitrate = str(self.audio_bitrate_combo.currentData())
        self.settings_model.metadata.cleanup_enabled = self.cleanup_check.isChecked()
        self.settings_model.metadata.set_filesystem_dates = self.filesystem_dates_check.isChecked()
        self.settings_model.metadata.stop_on_conflict = self.stop_on_conflict_check.isChecked()
        self.settings_model.metadata.rename_collision_use_subsec = self.collision_subsec_check.isChecked()
        self.settings_model.metadata.sanity_tolerance_seconds = self.tolerance_spin.value()
        self.settings_model.metadata.vacation_span_weeks = self.vacation_span_spin.value()
        self.settings_model.metadata.apply_mode = ApplyMode(str(self.apply_mode_combo.currentData()))

    def export_excel(self) -> None:
        """Export the current plan to an Excel workbook."""
        if not self.plans or self.root is None:
            return
        default_path = self.root / "vmp_preview.xlsx"
        selected, _ = QFileDialog.getSaveFileName(
            self,
            tr("Excel Report speichern"),
            str(default_path),
            "Excel Workbook (*.xlsx)",
        )
        if not selected:
            return
        try:
            export_excel_report(self.plans, Path(selected), self.settings_model)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Excel Export", str(exc))
            return
        QMessageBox.information(self, "Excel Export", tr("Report gespeichert."))

    def show_missing_exif(self) -> None:
        """Show files without core EXIF datetime values."""
        rows = missing_exif_rows(self.results)
        if not rows:
            QMessageBox.information(self, "Missing EXIF", tr("Keine Dateien ohne core EXIF-Datum gefunden."))
            return
        show_missing_exif_dialog(self, rows)

    @staticmethod
    def _pending_plan(item: MediaItem) -> MediaPlan:
        """Return a lightweight table row before metadata has been scanned."""
        result = AnalysisResult(
            item=item,
            metadata=RawMetadata(source_file=str(item.path), tags={}),
            resolved=ResolvedTimestamp(None, None, None, Confidence.LOW, tr("Scan ausstehend")),
            status=PlanStatus.SKIP,
            warnings=[tr("Scan ausstehend.")],
        )
        return MediaPlan(analysis=result)


def run_app() -> int:
    """Run the Qt application."""
    # Required before the QApplication exists so the Trip Lasso QtWebEngine map
    # renders (otherwise the embedded web view stays black).
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    # QtWebEngine's Chromium needs argv[0]; an empty list crashes it with
    # "the program name is not passed to QCoreApplication".
    app = QApplication(sys.argv if sys.argv else ["vacation-media-processor"])
    # Resolve the UI language before any window builds its (translated) strings.
    from PyQt6.QtCore import QLocale

    init_language(load_settings().language, QLocale.system().name())
    # Wrap the active style so tooltips appear a bit faster everywhere.
    app.setStyle(FastTooltipStyle(app.style()))
    icon_path = app_icon_path()
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    window = MainWindow()
    window.show()
    return app.exec()
