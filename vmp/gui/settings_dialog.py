"""Dialog for persistent tool-path settings."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QComboBox,
    QDialog,
    QStyle,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.i18n import tr
from ..core.logging_config import get_logger
from ..core.processes import command_template_error, launch_gui_tool, resolve_executable
from ..core.models import AppSettings
from ..core.settings import save_settings

LOGGER = get_logger(__name__)


class SettingsDialog(QDialog):
    """Dialog for configuring external tool paths."""

    def __init__(
        self,
        settings: AppSettings,
        parent: QWidget | None = None,
        root: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._root = root
        self.setWindowTitle(tr("Settings – Tools & Pfade"))
        self.setMinimumWidth(520)
        self.setModal(True)
        self._build_ui()
        self._apply_style()

    # ── Build ─────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        """Build the dialog layout."""
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(20, 20, 20, 20)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)

        cmd_tab = QWidget()
        cmd_layout = QVBoxLayout(cmd_tab)
        cmd_layout.setContentsMargins(0, 0, 0, 0)
        cmd_layout.setSpacing(12)

        # --- Pipeline Tools ---
        tools_box, tools_form = self._section("CMD Tools")
        hint = QLabel(tr("Diese Tools braucht die Automatik zum Scannen und Verarbeiten."))
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)
        tools_form.addRow("", hint)

        self.exiftool_edit = QLineEdit(self._settings.tools.exiftool)
        self.ffmpeg_edit = QLineEdit(self._settings.tools.ffmpeg)
        self.ffprobe_edit = QLineEdit(self._settings.tools.ffprobe)
        self.xnconvert_edit = QLineEdit(self._settings.tools.xnconvert)

        tools_form.addRow("ExifTool", self._path_row(self.exiftool_edit, "ExifTool"))
        tools_form.addRow("FFmpeg", self._path_row(self.ffmpeg_edit, "FFmpeg"))
        tools_form.addRow("FFprobe", self._path_row(self.ffprobe_edit, "FFprobe"))
        tools_form.addRow("NConvert", self._path_row(self.xnconvert_edit, "NConvert"))
        cmd_layout.addWidget(tools_box)
        cmd_layout.addStretch(1)
        tabs.addTab(cmd_tab, "CMD Tools")

        diff_tab = QWidget()
        diff_layout = QVBoxLayout(diff_tab)
        diff_layout.setContentsMargins(0, 0, 0, 0)
        diff_layout.setSpacing(12)

        # --- Diff Tools ---
        diff_box, diff_form = self._section(tr("Difftools"))
        diff_hint = QLabel(
            tr(
                "Vorlagen für Links/Rechts-Vergleiche. Verwende $source für links/Backup/Before "
                "und $target für rechts/aktuell/After."
            )
        )
        diff_hint.setObjectName("sectionHint")
        diff_hint.setWordWrap(True)
        diff_form.addRow("", diff_hint)
        self.image_diff_edit = QLineEdit(self._settings.diff_tools.image)
        self.video_diff_edit = QLineEdit(self._settings.diff_tools.video)
        self.text_diff_edit = QLineEdit(self._settings.diff_tools.text)
        diff_form.addRow(tr("Bilder"), self._template_row(self.image_diff_edit, tr("z. B. \"C:\\Tool\\imgdiff.exe\" \"$source\" \"$target\"")))
        diff_form.addRow("Videos", self._template_row(self.video_diff_edit, tr("z. B. \"C:\\Tool\\viddiff.exe\" /left \"$source\" /right \"$target\"")))
        diff_form.addRow(tr("Textdiff"), self._template_row(self.text_diff_edit, tr("z. B. \"C:\\Tool\\textdiff.exe\" \"$source\" \"$target\"")))
        diff_layout.addWidget(diff_box)
        diff_layout.addStretch(1)
        tabs.addTab(diff_tab, tr("Difftools"))

        gui_tab = QWidget()
        gui_layout = QVBoxLayout(gui_tab)
        gui_layout.setContentsMargins(0, 0, 0, 0)
        gui_layout.setSpacing(12)

        # --- Standalone GUI Tools ---
        head_box, head_form = self._section("Standalone GUI Tools")
        head_hint = QLabel(tr("Diese Programme sind Schnellstarter und werden von der Automatik nicht benötigt."))
        head_hint.setObjectName("sectionHint")
        head_hint.setWordWrap(True)
        head_form.addRow("", head_hint)

        self.xnconvert_gui_edit = QLineEdit(self._settings.tools.xnconvert_gui)
        self.xnviewmp_edit = QLineEdit(self._settings.tools.xnviewmp)
        self.shutter_edit = QLineEdit(self._settings.tools.shutter_encoder)
        self.table_font_spin = QSpinBox()
        self.table_font_spin.setRange(7, 20)
        self.table_font_spin.setValue(self._settings.table_font_size)
        self.table_font_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.table_font_spin.setSuffix(" pt")
        self.table_font_spin.valueChanged.connect(self._preview_table_font)
        self.lasso_cache_combo = QComboBox()
        self.lasso_cache_combo.addItem("RAM", "ram")
        self.lasso_cache_combo.addItem("Disk", "disk")
        self.lasso_cache_combo.addItem(tr("Aus"), "off")
        cache_index = self.lasso_cache_combo.findData(self._settings.lasso_thumbnail_cache_mode)
        self.lasso_cache_combo.setCurrentIndex(cache_index if cache_index >= 0 else 0)
        self.image_workers_spin = QSpinBox()
        self.image_workers_spin.setRange(1, 16)
        self.image_workers_spin.setValue(self._settings.images.parallel_workers)
        self.image_workers_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.lasso_workers_spin = QSpinBox()
        self.lasso_workers_spin.setRange(1, 12)
        self.lasso_workers_spin.setValue(self._settings.lasso_thumbnail_workers)
        self.lasso_workers_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.pair_workers_spin = QSpinBox()
        self.pair_workers_spin.setRange(1, 16)
        self.pair_workers_spin.setValue(self._settings.pair_check_workers)
        self.pair_workers_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.exiftool_batch_size_spin = QSpinBox()
        self.exiftool_batch_size_spin.setRange(1, 200)
        self.exiftool_batch_size_spin.setValue(self._settings.exiftool_read_batch_size)
        self.exiftool_batch_size_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.exiftool_parallel_batches_spin = QSpinBox()
        self.exiftool_parallel_batches_spin.setRange(1, 8)
        self.exiftool_parallel_batches_spin.setValue(self._settings.exiftool_parallel_batches)
        self.exiftool_parallel_batches_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)

        head_form.addRow(
            "XnConvert",
            self._launch_row(self.xnconvert_gui_edit, "XnConvert"),
        )
        head_form.addRow(
            "XnView MP",
            self._launch_row(self.xnviewmp_edit, "XnView MP"),
        )
        head_form.addRow(
            "Shutter Encoder",
            self._launch_row(self.shutter_edit, "Shutter Encoder"),
        )
        gui_layout.addWidget(head_box)
        gui_layout.addStretch(1)
        tabs.addTab(gui_tab, "Standalone GUI Tools")

        # --- Surface / UI ---
        surface_tab = QWidget()
        surface_layout = QVBoxLayout(surface_tab)
        surface_layout.setContentsMargins(0, 0, 0, 0)
        surface_layout.setSpacing(12)
        ui_box, ui_form = self._section(tr("Oberfläche"))
        ui_hint = QLabel(tr("UI-Optionen und Parallelität für interaktive Vorschauen, Bildverarbeitung und ExifTool-Reads."))
        ui_hint.setObjectName("sectionHint")
        ui_hint.setWordWrap(True)
        ui_form.addRow("", ui_hint)
        from PyQt6.QtCore import QLocale

        from ..core.i18n import available_languages, current_language, resolve_language

        def _language_display(code: str) -> str:
            native = QLocale(code).nativeLanguageName()
            return native[:1].upper() + native[1:] if native else code

        system_code = resolve_language("auto", QLocale.system().name())
        self.language_combo = QComboBox()
        self.language_combo.addItem(
            tr("Automatisch (System: {name})").format(name=_language_display(system_code)), "auto"
        )
        for code in available_languages():
            self.language_combo.addItem(_language_display(code), code)
        language_index = self.language_combo.findData(self._settings.language)
        self.language_combo.setCurrentIndex(max(language_index, 0))
        self.language_combo.setToolTip(
            tr(
                "Aktive Sprache / active language: {name}. "
                "Eine Änderung wird nach dem Speichern sofort übernommen."
            ).format(name=_language_display(current_language()))
        )
        self.folder_drop_combo = QComboBox()
        self.folder_drop_combo.addItem(tr("Nachfragen"), "ask")
        self.folder_drop_combo.addItem(tr("Ordner ergänzen"), "add")
        self.folder_drop_combo.addItem(tr("Vorhandene Liste ersetzen"), "replace")
        drop_index = self.folder_drop_combo.findData(self._settings.folder_drop_behavior)
        self.folder_drop_combo.setCurrentIndex(drop_index if drop_index >= 0 else 0)
        self.folder_drop_combo.setToolTip(
            tr("Legt fest, was beim Drag & Drop von Ordnern auf eine bereits geöffnete Dateiliste passiert.")
        )
        ui_form.addRow(tr("Sprache / Language"), self.language_combo)
        ui_form.addRow(tr("Drag & Drop bei geöffneter Liste"), self.folder_drop_combo)
        ui_form.addRow(tr("Tabellenschrift"), self._stepper_row(self.table_font_spin))
        ui_form.addRow("Lasso Thumbnail Cache", self.lasso_cache_combo)
        ui_form.addRow(tr("Bildverarbeitung parallel"), self._stepper_row(self.image_workers_spin))
        ui_form.addRow(tr("Lasso Thumbnails parallel"), self._stepper_row(self.lasso_workers_spin))
        ui_form.addRow(tr("Paarprüfung Thumbnails parallel"), self._stepper_row(self.pair_workers_spin))
        ui_form.addRow(tr("ExifTool Dateien pro Batch"), self._stepper_row(self.exiftool_batch_size_spin))
        ui_form.addRow(tr("ExifTool Batches parallel"), self._stepper_row(self.exiftool_parallel_batches_spin))
        surface_layout.addWidget(ui_box)
        surface_layout.addStretch(1)
        tabs.addTab(surface_tab, "Misc")

        # --- Buttons ---
        btn_layout = QHBoxLayout()
        btn_layout.addStretch(1)

        save_btn = QPushButton(tr("Speichern"))
        save_btn.setObjectName("primaryButton")
        save_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
        save_btn.clicked.connect(self._on_save)
        btn_layout.addWidget(save_btn)

        cancel_btn = QPushButton(tr("Abbrechen"))
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)

    def _section(self, title: str) -> tuple[QFrame, QFormLayout]:
        """Create a titled section panel."""
        section = QFrame()
        section.setObjectName("sideSection")
        sec_layout = QVBoxLayout(section)
        sec_layout.setContentsMargins(14, 12, 14, 14)
        sec_layout.setSpacing(10)
        label = QLabel(title)
        label.setObjectName("sectionTitle")
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(9)
        sec_layout.addWidget(label)
        sec_layout.addLayout(form)
        return section, form

    def _apply_style(self) -> None:
        """Apply the same restrained style as the main window."""
        self.setStyleSheet("""
            QDialog, QWidget {
                background: #eef1f5; color: #1e242c; font-size: 13px;
            }
            QLabel, QCheckBox { background: transparent; }
            QLineEdit {
                background: #ffffff; border: 1px solid #cfd6e3;
                border-radius: 4px; padding: 4px 8px; color: #1f2937;
            }
            QPushButton {
                background: #e8edf4; border: 1px solid #cfd6e3;
                border-radius: 5px; padding: 6px 14px; color: #1f2937;
            }
            QPushButton:hover {
                background: #dce3ee; border-color: #aab4c7;
            }
            QPushButton#primaryButton {
                background: #2f6fed; color: #ffffff;
                border: none; padding: 6px 20px;
            }
            QPushButton#primaryButton:hover {
                background: #1f5fd9;
            }
            QPushButton#miniButton {
                background: transparent; border: 1px solid #cfd6e3;
                border-radius: 4px; padding: 2px;
            }
            QPushButton#assistantButton {
                background: #e8edf4; border: 1px solid #cfd6e3;
                border-radius: 4px; padding: 2px 6px;
            }
            QFrame#sideSection {
                background: #ffffff; border: 1px solid #dfe6ef;
                border-radius: 8px;
            }
            QLabel#sectionTitle {
                font-size: 14px; font-weight: 600; color: #111827;
            }
            QLabel#sectionHint {
                font-size: 12px; color: #6b7280; font-style: italic;
            }
        """)

    # ── Widget helpers ────────────────────────────────────────────────

    def _path_row(self, edit: QLineEdit, display_name: str) -> QWidget:
        """Build a path field with browse button."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        edit.setMinimumWidth(0)
        btn = QPushButton("...")
        btn.setObjectName("miniButton")
        btn.setFixedWidth(34)
        btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        btn.setIconSize(QSize(16, 16))
        btn.setText("")
        btn.clicked.connect(
            lambda checked=False, e=edit, n=display_name: self._browse(e, n)
        )
        layout.addWidget(edit, 1)
        layout.addWidget(btn)
        return row

    def _launch_row(self, edit: QLineEdit, display_name: str) -> QWidget:
        """Build a path field with browse and launch buttons."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        edit.setMinimumWidth(0)

        browse_btn = QPushButton("")
        browse_btn.setObjectName("miniButton")
        browse_btn.setFixedWidth(32)
        browse_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        browse_btn.setIconSize(QSize(15, 15))
        browse_btn.clicked.connect(lambda: self._browse(edit, display_name))

        launch_btn = QPushButton("▶")
        launch_btn.setObjectName("assistantButton")
        launch_btn.setToolTip(tr("{name} öffnen").format(name=display_name))
        launch_btn.setFixedWidth(34)
        # pass_root=True only for XnView MP, False for the others
        pass_root = display_name == "XnView MP"
        launch_btn.clicked.connect(
            lambda checked=False, e=edit, n=display_name, p=pass_root: self._launch_tool(e, n, p)
        )

        layout.addWidget(edit, 1)
        layout.addWidget(browse_btn)
        layout.addWidget(launch_btn)
        return row

    def _template_row(self, edit: QLineEdit, placeholder: str) -> QWidget:
        """Build a command-template row for diff tools."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        edit.setMinimumWidth(0)
        edit.setPlaceholderText(placeholder)
        layout.addWidget(edit, 1)
        return row

    def _stepper_row(self, spinbox: QSpinBox) -> QWidget:
        """Build a compact spinbox row with explicit minus/plus buttons."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        minus_button = QPushButton("-")
        plus_button = QPushButton("+")
        for button in (minus_button, plus_button):
            button.setObjectName("miniButton")
            button.setFixedSize(26, 28)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        minus_button.clicked.connect(spinbox.stepDown)
        plus_button.clicked.connect(spinbox.stepUp)
        layout.addWidget(spinbox, 1)
        layout.addWidget(minus_button)
        layout.addWidget(plus_button)
        return row

    def _preview_table_font(self, size: int) -> None:
        """Preview the table font live in the parent main window."""
        parent = self.parent()
        if parent is None:
            return
        preview = getattr(parent, "_apply_table_font_size", None)
        if callable(preview):
            preview(size)

    # ── Actions ───────────────────────────────────────────────────────

    def _browse(self, edit: QLineEdit, display_name: str) -> None:
        """Open a file picker for a tool executable."""
        LOGGER.info("Choosing tool path title=%s current=%s", display_name, edit.text())
        selected, _ = QFileDialog.getOpenFileName(
            self, tr("{name} wählen").format(name=display_name), edit.text(),
            tr("Executables (*.exe);;Alle Dateien (*)"),
        )
        if selected:
            LOGGER.info("Tool path selected title=%s selected=%s", display_name, selected)
            edit.setText(selected)
        else:
            LOGGER.info("Tool path selection cancelled title=%s", display_name)

    def _launch_tool(self, edit: QLineEdit, display_name: str, pass_root: bool) -> None:
        """Launch a configured GUI tool (same logic as MainWindow's helper)."""
        executable = edit.text().strip()
        LOGGER.info(
            "Launch requested display_name=%s executable=%s pass_root=%s",
            display_name, executable, pass_root,
        )
        if not executable:
            QMessageBox.warning(
                self,
                display_name,
                tr("{name} wurde nicht gefunden. Bitte in Settings konfigurieren oder installieren.").format(
                    name=display_name
                ),
            )
            return

        resolved = resolve_executable(executable)
        if resolved is None:
            QMessageBox.warning(
                self,
                display_name,
                tr("{name} wurde nicht gefunden. Bitte in Settings konfigurieren oder installieren.").format(
                    name=display_name
                ),
            )
            return

        extra_args = [str(self._root)] if pass_root and self._root is not None else []
        try:
            launch_gui_tool(resolved, extra_args)
        except OSError as exc:
            LOGGER.exception("Could not launch %s", display_name)
            QMessageBox.critical(
                self, display_name,
                tr("{name} konnte nicht gestartet werden:\n{error}").format(name=display_name, error=exc),
            )

    def _on_save(self) -> None:
        """Persist tool paths and close."""
        diff_templates = (
            (tr("Bilder-Difftool"), self.image_diff_edit.text().strip()),
            (tr("Video-Difftool"), self.video_diff_edit.text().strip()),
            (tr("Textdiff"), self.text_diff_edit.text().strip()),
        )
        for label, template in diff_templates:
            if not template:
                continue
            error = command_template_error(template)
            if error is not None:
                LOGGER.warning("Invalid %s template: %s", label, error.replace("\n", " "))
                QMessageBox.warning(
                    self,
                    tr("{name} ungültig").format(name=label),
                    tr(
                        "{name} kann so nicht gespeichert werden.\n\n{error}\n\n"
                        "Bitte die konkrete .exe angeben, nicht nur den Tool-Ordner."
                    ).format(name=label, error=error),
                )
                return
        self._settings.tools.exiftool = self.exiftool_edit.text().strip() or "exiftool"
        self._settings.tools.ffmpeg = self.ffmpeg_edit.text().strip() or "ffmpeg"
        self._settings.tools.ffprobe = self.ffprobe_edit.text().strip() or "ffprobe"
        self._settings.tools.xnconvert = self.xnconvert_edit.text().strip()
        self._settings.tools.xnconvert_gui = self.xnconvert_gui_edit.text().strip()
        self._settings.tools.xnviewmp = self.xnviewmp_edit.text().strip()
        self._settings.tools.shutter_encoder = self.shutter_edit.text().strip()
        self._settings.diff_tools.image = self.image_diff_edit.text().strip()
        self._settings.diff_tools.video = self.video_diff_edit.text().strip()
        self._settings.diff_tools.text = self.text_diff_edit.text().strip()
        self._settings.language = str(self.language_combo.currentData())
        self._settings.folder_drop_behavior = str(self.folder_drop_combo.currentData())
        self._settings.table_font_size = self.table_font_spin.value()
        self._settings.lasso_thumbnail_cache_mode = str(self.lasso_cache_combo.currentData())
        self._settings.images.parallel_workers = self.image_workers_spin.value()
        self._settings.lasso_thumbnail_workers = self.lasso_workers_spin.value()
        self._settings.pair_check_workers = self.pair_workers_spin.value()
        self._settings.exiftool_read_batch_size = self.exiftool_batch_size_spin.value()
        self._settings.exiftool_parallel_batches = self.exiftool_parallel_batches_spin.value()
        save_settings(self._settings)
        LOGGER.info("Settings saved from dialog")
        self.accept()
