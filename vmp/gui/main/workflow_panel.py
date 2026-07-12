"""Builder for the main window's Workflow settings sidebar section.

Creates every workflow control, attaches it as an attribute on the given
``MainWindow`` (the window remains the owner of state and signal handlers),
and returns the finished collapsible section widget.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import QHBoxLayout
from PyQt6.QtWidgets import QCheckBox, QComboBox, QPushButton, QSpinBox, QStyle, QWidget

from ...core.i18n import tr
from ..common.form_rows import (
    checkbox_pair_row,
    checkbox_with_info,
    collapsible_section,
    combo_pair_row,
    label_info_row,
    stepper_pair_row,
    stepper_triple_row,
    style_spinbox,
)
from ...core.models import ApplyMode

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .window import MainWindow


def build_workflow_section(window: "MainWindow") -> QWidget:
    """Create the Workflow box, attach its controls to ``window``, return the box."""
    workflow_box, form = collapsible_section("Workflow")
    window.recursive_check = QCheckBox(tr("Unterordner einbeziehen"))
    window.recursive_check.setChecked(window.settings_model.recursive)
    window.skip_backup_check = QCheckBox(tr("Backup überspringen"))
    window.skip_backup_check.setChecked(window.settings_model.skip_backup)
    window.skip_backup_check.setToolTip(tr("Keine Sicherungskopie vor dem Ersetzen, Verschieben oder Löschen erstellen."))
    window.read_after_exif_check = QCheckBox(tr("Before/After-EXIF JSON schreiben"))
    window.read_after_exif_check.setChecked(window.settings_model.read_after_exif)
    window.read_after_exif_check.setToolTip(
        tr("Nach Apply die finalen Dateien erneut lesen und vergleichbare Before/After-JSONs schreiben.")
    )
    window.readback_diff_button = QPushButton("")
    window.readback_diff_button.setObjectName("miniButton")
    window.readback_diff_button.setIcon(window.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
    window.readback_diff_button.setIconSize(QSize(13, 13))
    window.readback_diff_button.setFixedSize(24, 20)
    window.readback_diff_button.setToolTip(tr("Before/After-JSON mit Textdiff öffnen"))
    window.readback_diff_button.setEnabled(False)
    window.readback_diff_button.clicked.connect(window.open_readback_diff)
    window.heic_heif_convert_check = QCheckBox(tr("HEIC/HEIF zu JPG konvertieren"))
    window.heic_heif_convert_check.setChecked(window.settings_model.images.heic_heif_to_jpg)
    window.heic_heif_convert_check.toggled.connect(window._on_heic_heif_toggle_changed)
    window.skip_depth_heic_check = QCheckBox(tr("HEIC mit Tiefendaten (Depthmap) behalten"))
    window.skip_depth_heic_check.setToolTip(
        tr(
            "HEIC/HEIF mit editierbarer Tiefen-/Portrait-Map nicht nach JPG flatten – "
            "HEIC bleibt HEIC (wird aber weiterhin umbenannt/normalisiert)."
        )
    )
    window.skip_depth_heic_check.setChecked(window.settings_model.images.skip_depth_heic_conversion)
    window.skip_depth_heic_check.toggled.connect(window._on_workflow_settings_changed)
    window.preserve_depth_gdepth_check = QCheckBox(tr("Tiefe beim Konvertieren als GDepth ins JPG einbetten"))
    window.preserve_depth_gdepth_check.setToolTip(
        tr(
            "Beim HEIC→JPG-Konvertieren die Tiefen-Map als Google-GDepth (XMP) ins JPG einbetten, "
            "damit sie später weiterverwendbar bleibt."
        )
    )
    window.preserve_depth_gdepth_check.setChecked(window.settings_model.images.preserve_depth_as_gdepth)
    window.preserve_depth_gdepth_check.toggled.connect(window._on_workflow_settings_changed)
    window.png_convert_check = QCheckBox(tr("PNGs auch zu JPG konvertieren"))
    window.png_convert_check.setChecked(window.settings_model.images.png_to_jpg)
    window.png_convert_check.toggled.connect(window._on_workflow_settings_changed)
    window.quality_spin = QSpinBox()
    window.quality_spin.setRange(1, 100)
    window.quality_spin.setValue(window.settings_model.images.jpeg_quality)
    style_spinbox(window.quality_spin, "%")
    window.quality_spin.valueChanged.connect(window._on_workflow_settings_changed)
    window.heic_heif_quality_spin = QSpinBox()
    window.heic_heif_quality_spin.setRange(1, 100)
    window.heic_heif_quality_spin.setValue(window.settings_model.images.heic_heif_jpeg_quality)
    style_spinbox(window.heic_heif_quality_spin, "%")
    window.heic_heif_quality_spin.valueChanged.connect(window._on_workflow_settings_changed)
    window.heic_heif_quality_row = stepper_pair_row("JPEGli", window.quality_spin, "HEIC/HEIF -> JPGli", window.heic_heif_quality_spin)
    window.jpeg_rotate_check = QCheckBox(tr("JPG: nach EXIF drehen"))
    window.jpeg_rotate_check.setChecked(window.settings_model.images.jpeg_rotate_by_exif)
    window.jpeg_thumb_check = QCheckBox(tr("JPG: EXIF-Thumbnail rebuild"))
    window.jpeg_thumb_check.setChecked(window.settings_model.images.jpeg_rebuild_exif_thumbnail)
    window.fhd_spin = QSpinBox()
    window.fhd_spin.setRange(10, 40)
    window.fhd_spin.setValue(window.settings_model.videos.fhd_crf)
    style_spinbox(window.fhd_spin, " CRF")
    window.fhd_spin.valueChanged.connect(window._on_workflow_settings_changed)
    window.qhd_spin = QSpinBox()
    window.qhd_spin.setRange(10, 40)
    window.qhd_spin.setValue(window.settings_model.videos.qhd_crf)
    style_spinbox(window.qhd_spin, " CRF")
    window.qhd_spin.valueChanged.connect(window._on_workflow_settings_changed)
    window.uhd_spin = QSpinBox()
    window.uhd_spin.setRange(10, 40)
    window.uhd_spin.setValue(window.settings_model.videos.uhd_crf)
    style_spinbox(window.uhd_spin, " CRF")
    window.uhd_spin.valueChanged.connect(window._on_workflow_settings_changed)
    window.qhd_threshold_spin = QSpinBox()
    window.qhd_threshold_spin.setRange(720, 7999)
    window.qhd_threshold_spin.setSingleStep(100)
    window.qhd_threshold_spin.setValue(window.settings_model.videos.qhd_long_edge_threshold)
    style_spinbox(window.qhd_threshold_spin, " px")
    window.qhd_threshold_spin.setToolTip(tr("Ab dieser längsten Kante nutzt der Plan den QHD-CRF."))
    window.uhd_threshold_spin = QSpinBox()
    window.uhd_threshold_spin.setRange(721, 8000)
    window.uhd_threshold_spin.setSingleStep(100)
    window.uhd_threshold_spin.setValue(window.settings_model.videos.uhd_long_edge_threshold)
    style_spinbox(window.uhd_threshold_spin, " px")
    window.uhd_threshold_spin.setToolTip(tr("Ab dieser längsten Kante nutzt der Plan den 4K-CRF."))
    window.qhd_threshold_spin.valueChanged.connect(
        lambda value: window._sync_video_bucket_threshold_limits("qhd", value)
    )
    window.qhd_threshold_spin.valueChanged.connect(window._on_workflow_settings_changed)
    window.uhd_threshold_spin.valueChanged.connect(
        lambda value: window._sync_video_bucket_threshold_limits("uhd", value)
    )
    window.uhd_threshold_spin.valueChanged.connect(window._on_workflow_settings_changed)
    window._sync_video_bucket_threshold_limits("qhd", window.qhd_threshold_spin.value())
    window.limit_fhd_check = QCheckBox(tr("Video auf Full HD begrenzen"))
    window.limit_fhd_check.setChecked(window.settings_model.videos.limit_to_fhd)
    window.limit_fhd_check.setToolTip(
        tr(
            "Wenn aktiv, werden Videos über Full HD zusätzlich auf max. 1920x1080 skaliert, "
            "bei gleichem Seitenverhältnis."
        )
    )
    window.limit_fhd_check.toggled.connect(window._on_workflow_settings_changed)
    window.audio_codec_combo = QComboBox()
    window.audio_codec_combo.addItem("AAC", "aac")
    window.audio_codec_combo.addItem("AC3", "ac3")
    window.audio_codec_combo.addItem(tr("Original behalten"), "copy")
    window.audio_codec_combo.setToolTip(
        tr(
            "Audio-Codec der Tonspur beim Transcode. AAC ist effizient und sehr breit kompatibel; "
            "AC3 ist v. a. für 5.1/TV interessant; 'Original behalten' kopiert die Tonspur unverändert."
        )
    )
    current_audio_index = window.audio_codec_combo.findData(window.settings_model.videos.audio_codec)
    window.audio_codec_combo.setCurrentIndex(max(current_audio_index, 0))
    window.audio_codec_combo.currentIndexChanged.connect(window._on_workflow_settings_changed)
    window.audio_bitrate_combo = QComboBox()
    for bitrate in ("96k", "128k", "160k", "192k", "256k", "320k"):
        window.audio_bitrate_combo.addItem(bitrate, bitrate)
    current_bitrate_index = window.audio_bitrate_combo.findData(window.settings_model.videos.audio_bitrate)
    window.audio_bitrate_combo.setCurrentIndex(max(current_bitrate_index, 0))
    window.audio_bitrate_combo.setToolTip(tr("Audio-Bitrate, wenn die Tonspur neu kodiert wird."))
    window.audio_bitrate_combo.currentIndexChanged.connect(window._on_workflow_settings_changed)
    window.audio_codec_combo.currentIndexChanged.connect(window._sync_audio_bitrate_state)
    window._sync_audio_bitrate_state()
    window._sync_heic_heif_quality_state(window.heic_heif_convert_check.isChecked())
    window.cleanup_check = QCheckBox(tr("Junk-Tags entfernen"))
    window.cleanup_check.setChecked(window.settings_model.metadata.cleanup_enabled)
    window.cleanup_check.toggled.connect(window._on_workflow_settings_changed)
    window.filesystem_dates_check = QCheckBox(tr("File Creation / Modify Dates auf Zieldatum setzen"))
    window.filesystem_dates_check.setChecked(window.settings_model.metadata.set_filesystem_dates)
    window.filesystem_dates_check.setToolTip(
        tr("Setzt die echten Dateisystem-Zeitstempel auf die lokale Aufnahmezeit, passend zum Dateinamen.")
    )
    window.filesystem_dates_check.toggled.connect(window._on_workflow_settings_changed)
    window.stop_on_conflict_check = QCheckBox(tr("Apply bei ungelösten Dateien blockieren"))
    window.stop_on_conflict_check.setChecked(window.settings_model.metadata.stop_on_conflict)
    window.stop_on_conflict_check.toggled.connect(window._on_workflow_settings_changed)
    window.collision_subsec_check = QCheckBox(tr("Namenskollision: Millisekunden statt -Zähler"))
    window.collision_subsec_check.setChecked(window.settings_model.metadata.rename_collision_use_subsec)
    window.collision_subsec_check.setToolTip(
        tr(
            "Bei gleichem Zeitstempel-Namen: falls das Bild Sub-Sekunden (Millisekunden) enthält, "
            "diese als Suffix nutzen (z.B. 20260505_155603.450ms.jpg) statt -2, -3 …"
        )
    )
    window.collision_subsec_check.toggled.connect(window._on_workflow_settings_changed)
    window.apply_mode_combo = QComboBox()
    window.apply_mode_combo.addItem("Full normalize", ApplyMode.FULL_NORMALIZE.value)
    window.apply_mode_combo.addItem("Rename only", ApplyMode.RENAME_ONLY.value)
    window.apply_mode_combo.addItem("Samsung cleanup only", ApplyMode.SAMSUNG_CLEANUP.value)
    window.apply_mode_combo.addItem("Samsung + GPS cleanup", ApplyMode.GPS_CLEANUP.value)
    current_mode_index = window.apply_mode_combo.findData(window.settings_model.metadata.apply_mode.value)
    window.apply_mode_combo.setCurrentIndex(max(current_mode_index, 0))
    window.apply_mode_combo.currentIndexChanged.connect(window._on_workflow_settings_changed)
    window.tolerance_spin = QSpinBox()
    window.tolerance_spin.setRange(0, 3600)
    window.tolerance_spin.setValue(window.settings_model.metadata.sanity_tolerance_seconds)
    style_spinbox(window.tolerance_spin, " s")
    window.tolerance_spin.valueChanged.connect(window._on_workflow_settings_changed)
    window.vacation_span_spin = QSpinBox()
    window.vacation_span_spin.setRange(0, 104)
    window.vacation_span_spin.setValue(window.settings_model.metadata.vacation_span_weeks)
    style_spinbox(window.vacation_span_spin, tr(" Wochen"))
    window.vacation_span_spin.valueChanged.connect(window._on_workflow_settings_changed)
    form.addRow("", window.recursive_check)
    form.addRow(
        "",
        checkbox_with_info(
            window.heic_heif_convert_check,
            tr("Nur wenn aktiv, werden HEIC- und HEIF-Quellen beim Full-Normalize-Lauf nach JPG konvertiert."),
        ),
    )
    form.addRow(
        "",
        checkbox_with_info(
            window.skip_depth_heic_check,
            tr(
                "HEIC/HEIF mit editierbarer Tiefen-/Portrait-Map (Depthmap) werden NICHT nach JPG geflattet, "
                "sondern bleiben HEIC (weiterhin umbenannt/normalisiert), damit die Tiefendaten erhalten bleiben."
            ),
        ),
    )
    form.addRow(
        "",
        checkbox_with_info(
            window.preserve_depth_gdepth_check,
            tr(
                "Wird eine Tiefen-HEIC doch nach JPG konvertiert, wird die Tiefen-Map als Google-GDepth (XMP) "
                "ins JPG eingebettet und bleibt so weiterverwendbar."
            ),
        ),
    )
    form.addRow(
        "",
        checkbox_with_info(
            window.png_convert_check,
            tr(
                "Nur wenn aktiv, werden PNG-Quellen beim Full-Normalize-Lauf nach JPG konvertiert. "
                "Sonst bleiben PNGs im PNG-Format (werden aber ggf. weiter normalisiert/umbenannt)."
            ),
        ),
    )
    form.addRow("", window.heic_heif_quality_row)
    form.addRow("JPEG", checkbox_pair_row(window.jpeg_rotate_check, window.jpeg_thumb_check))
    form.addRow(
        label_info_row(
            tr("Video CRF und Resolution Buckets"),
            tr("Die CRF-Werte und Bucket-Grenzen steuern, welche Video-Kompression bei welcher Auflösung verwendet wird."),
        ),
        stepper_triple_row("FHD", window.fhd_spin, "QHD", window.qhd_spin, "4K", window.uhd_spin),
    )
    form.addRow("", stepper_pair_row(tr("QHD ab"), window.qhd_threshold_spin, tr("4K ab"), window.uhd_threshold_spin))
    form.addRow("", checkbox_with_info(window.limit_fhd_check, tr("Videos über Full HD werden beim Apply zusätzlich heruntergerechnet, ohne das Seitenverhältnis zu verändern.")))
    form.addRow(
        "",
        combo_pair_row(tr("Audio-Codec"), window.audio_codec_combo, "Bitrate", window.audio_bitrate_combo),
    )
    form.addRow(
        label_info_row(
            "Apply Mode",
            tr(
                "Steuert, welche Arbeitsschritte der Lauf überhaupt ausführen darf:\n"
                "• Full normalize: Bild-/Video-Konvertierung, Metadaten-Schreiben und Rename\n"
                "• Rename only: nur Metadaten-Schreiben und Umbenennen, keine Medien-Konvertierung\n"
                "• Samsung cleanup only: nur Samsung-spezifische Aufräumtags und keine Umwandlung\n"
                "• Samsung + GPS cleanup: Samsung- und GPS-Bereinigung, ebenfalls ohne Medien-Konvertierung"
            ),
        ),
        window.apply_mode_combo,
    )
    form.addRow(
        label_info_row(
            tr("Metadaten-Checks"),
            tr("Steuert die Toleranz für Zeitstempel-Abweichungen und den Prüfzeitraum für geplante Aufnahmen."),
        ),
        stepper_pair_row(tr("Toleranz"), window.tolerance_spin, tr("Zeitraum"), window.vacation_span_spin),
    )
    form.addRow("", checkbox_with_info(window.skip_backup_check, tr("Keine Sicherungskopie vor dem Ersetzen, Verschieben oder Löschen erstellen.")))
    form.addRow(
        "",
        window._readback_diff_row(
            tr("Die finalen Dateien nach Apply erneut per ExifTool lesen und zwei gleich strukturierte JSONs für Links/Rechts-Diff schreiben.")
        ),
    )
    form.addRow(
        "",
        checkbox_with_info(
            window.cleanup_check,
            tr(
                "Entfernt beim Schreiben typische Metadaten-Reste wie XMP-xmp:CreateDate, XMP-xmp:ModifyDate, "
                "XMP-photoshop:DateCreated, GPS:GPSDateStamp, GPS:GPSTimeStamp; bei Videos zusätzlich Trailer:* "
                "und Samsung:Trailer*, bei aggressivem Cleanup zusätzlich XMP:All."
            ),
        ),
    )
    form.addRow(
        "",
        checkbox_with_info(
            window.filesystem_dates_check,
            tr(
                "Setzt FileCreateDate/FileModifyDate und die echten Dateisystem-Zeitstempel auf die lokale Aufnahmezeit. "
                "Die Uhrzeit entspricht der Local-Zeit, die auch für den Ziel-Dateinamen verwendet wird; unter Windows "
                "wird zusätzlich die Creation Time gesetzt, auf Unix-Systemen mindestens Modify/Access Time."
            ),
        ),
    )
    form.addRow("", checkbox_with_info(window.stop_on_conflict_check, tr("Blockiert Apply, wenn Dateien mit ungelöster Zeitstempel-Situation gefunden werden.")))
    form.addRow(
        "",
        checkbox_with_info(
            window.collision_subsec_check,
            tr(
                "Bei gleichem Zeitstempel-Dateinamen wird – sofern das Bild Millisekunden (Sub-Sekunden) enthält – "
                "ein Suffix wie .450ms verwendet (z.B. 20260505_155603.450ms.jpg) statt -2, -3 … "
                "Ohne Millisekunden bleibt der numerische Zähler."
            ),
        ),
    )
    return workflow_box


class WorkflowSettingsMixin:
    """Signal handlers that keep the workflow sidebar controls consistent."""

    def _sync_audio_bitrate_state(self) -> None:
        """Disable the bitrate dropdown when the audio track is kept as-is."""
        keep_original = self.audio_codec_combo.currentData() == "copy"
        self.audio_bitrate_combo.setEnabled(not keep_original)

    def _readback_diff_row(self, tooltip: str) -> QWidget:
        """Build the readback checkbox row with a post-run diff button."""
        row = checkbox_with_info(self.read_after_exif_check, tooltip)
        layout = row.layout()
        if isinstance(layout, QHBoxLayout):
            layout.addWidget(self.readback_diff_button, 0, Qt.AlignmentFlag.AlignTop)
        return row

    def _sync_heic_heif_quality_state(self, enabled: bool) -> None:
        """Enable or disable HEIC/HEIF conversion and its quality control."""
        self.heic_heif_quality_spin.setEnabled(enabled)
        if hasattr(self, "heic_heif_quality_row"):
            second_group = getattr(self.heic_heif_quality_row, "second_group", None)
            if second_group is not None:
                second_group.setEnabled(enabled)

    def _on_heic_heif_toggle_changed(self, enabled: bool) -> None:
        """Update HEIC/HEIF controls and refresh plans when the toggle changes."""
        self._sync_heic_heif_quality_state(enabled)
        self._schedule_workflow_refresh()

    def _on_workflow_settings_changed(self, *_args: object) -> None:
        """Refresh plans and the action column after live workflow setting changes."""
        self._schedule_workflow_refresh()

    def _schedule_workflow_refresh(self) -> None:
        """Debounce workflow changes before rebuilding all plans."""
        self._workflow_refresh_timer.start()

    def _apply_workflow_refresh(self) -> None:
        """Apply the latest workflow settings to the current plan list."""
        self._sync_settings_from_ui()
        self._refresh_plans_from_results()

    def _sync_video_bucket_threshold_limits(self, changed: str, value: int) -> None:
        """Keep the video bucket thresholds ordered without making the UI brittle."""
        if changed == "qhd":
            self.uhd_threshold_spin.setMinimum(max(721, value + 1))
            if self.uhd_threshold_spin.value() <= value:
                self.uhd_threshold_spin.blockSignals(True)
                self.uhd_threshold_spin.setValue(min(self.uhd_threshold_spin.maximum(), value + 1))
                self.uhd_threshold_spin.blockSignals(False)
            return
        self.qhd_threshold_spin.setMaximum(min(7999, value - 1))
        if self.qhd_threshold_spin.value() >= value:
            self.qhd_threshold_spin.blockSignals(True)
            self.qhd_threshold_spin.setValue(max(self.qhd_threshold_spin.minimum(), value - 1))
            self.qhd_threshold_spin.blockSignals(False)

