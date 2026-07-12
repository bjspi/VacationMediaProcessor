"""Builder for the main window's branded top action bar."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QHBoxLayout, QFrame, QLabel, QPushButton, QStyle, QVBoxLayout, QWidget

from ...core.i18n import tr
from ..common.theme import app_icon_path
from ..common.widgets import BadgeHeaderButton

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .window import MainWindow


def _style_header_button(button: QPushButton) -> None:
    """Make top action buttons compact and non-clipping."""
    if button.text():
        button.setAccessibleName(button.text())
    button.setText("")
    button.setProperty("headerAction", True)
    button.setFixedSize(44, 36)
    button.setIconSize(QSize(20, 20))


def build_header(window: "MainWindow") -> QWidget:
    """Create the header bar; every action button is attached to ``window``."""
    header = QFrame()
    header.setObjectName("header")
    header_layout = QVBoxLayout(header)
    header_layout.setContentsMargins(14, 12, 14, 12)
    header_layout.setSpacing(10)
    brand_row = QHBoxLayout()
    brand_row.setSpacing(12)
    action_row = QHBoxLayout()
    action_row.setSpacing(6)

    logo = QLabel()
    logo.setObjectName("logo")
    icon_path = app_icon_path()
    if icon_path.exists():
        logo.setPixmap(
            QPixmap(str(icon_path)).scaled(
                52,
                52,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
    logo.setFixedSize(56, 56)
    logo.setAlignment(Qt.AlignmentFlag.AlignCenter)

    title_box = QVBoxLayout()
    title_box.setSpacing(2)
    title = QLabel("Vacation Media Processor")
    title.setObjectName("appTitle")
    subtitle = QLabel(tr("Fotos, Videos, EXIF und Rename in einem sicheren Batch-Workflow"))
    subtitle.setObjectName("appSubtitle")
    window.folder_label = QLabel(tr("Kein Ordner geöffnet"))
    window.folder_label.setObjectName("folderLabel")
    title_box.addWidget(title)
    title_box.addWidget(subtitle)
    title_box.addWidget(window.folder_label)

    open_button = QPushButton(tr("Ordner"))
    open_button.setObjectName("primaryButton")
    open_button.setIcon(window.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
    open_button.setToolTip(tr("Ordner öffnen"))
    open_button.clicked.connect(window.choose_folder)
    window.scan_button = QPushButton("Scan")
    window.scan_button.setObjectName("secondaryButton")
    window.scan_button.setIcon(window.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
    window.scan_button.setToolTip(tr("Medien scannen und Plan bauen"))
    window.scan_button.clicked.connect(lambda: window.scan())
    window.run_button = QPushButton("Run")
    window.run_button.setObjectName("runButton")
    window.run_button.setIcon(window.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
    window.run_button.setToolTip(tr("Alle geplanten Schritte ausführen"))
    window.run_button.clicked.connect(window.run_all)
    window.run_button.setEnabled(False)
    window.run_images_button = QPushButton("Img")
    window.run_images_button.setObjectName("secondaryButton")
    window.run_images_button.setIcon(window.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon))
    window.run_images_button.setToolTip(tr("Nur Bild-Pläne ausführen"))
    window.run_images_button.clicked.connect(window.run_images)
    window.run_images_button.setEnabled(False)
    window.run_videos_button = QPushButton("Vid")
    window.run_videos_button.setObjectName("secondaryButton")
    window.run_videos_button.setIcon(window.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
    window.run_videos_button.setToolTip(tr("Nur Video-Pläne ausführen"))
    window.run_videos_button.clicked.connect(window.run_videos)
    window.run_videos_button.setEnabled(False)
    window.run_selected_button = QPushButton("Sel")
    window.run_selected_button.setObjectName("secondaryButton")
    window.run_selected_button.setIcon(window.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
    window.run_selected_button.setToolTip(tr("Ausgewählte Dateien verarbeiten"))
    window.run_selected_button.clicked.connect(window.run_selected)
    window.run_selected_button.setEnabled(False)
    window.export_button = QPushButton("XLS")
    window.export_button.setObjectName("ghostButton")
    window.export_button.setIcon(window.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
    window.export_button.setToolTip(tr("Excel-Report exportieren"))
    window.export_button.clicked.connect(window.export_excel)
    window.export_button.setEnabled(False)
    window.missing_button = BadgeHeaderButton("0", window)
    window.missing_button.setAccessibleName("EXIF")
    window.missing_button.setObjectName("ghostButton")
    window.missing_button.setIcon(window.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning))
    window.missing_button.setToolTip(tr("Dateien ohne core EXIF-Datum anzeigen"))
    window.missing_button.clicked.connect(window.show_missing_exif)
    window.missing_button.setEnabled(False)
    window.jpeg_fix_button = QPushButton("JPG")
    window.jpeg_fix_button.setObjectName("ghostButton")
    window.jpeg_fix_button.setIcon(window._asset_icon("rotate.svg"))
    window.jpeg_fix_button.setToolTip(tr("JPG Fix: EXIF-Rotation und EXIF-Thumbnail neu bauen"))
    window.jpeg_fix_button.clicked.connect(window.run_jpeg_maintenance)
    window.jpeg_fix_button.setEnabled(False)
    window.explorer_button = QPushButton("EX")
    window.explorer_button.setObjectName("ghostButton")
    window.explorer_button.setIcon(window.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
    window.explorer_button.setToolTip(tr("Den Ordner der aktiven Datei im Explorer öffnen"))
    window.explorer_button.clicked.connect(window.open_active_item_location)
    window.lasso_button = QPushButton("Trip")
    window.lasso_button.setObjectName("ghostButton")
    window.lasso_button.setIcon(window._asset_icon("lasso.svg"))
    window.lasso_button.setToolTip(tr("Reise-Lasso: Medien per Karte/Datum auswählen und in einen Ordner verschieben"))
    window.lasso_button.clicked.connect(window.open_trip_lasso)
    window.lasso_button.setEnabled(False)
    window.pairs_button = BadgeHeaderButton("0", window)
    window.pairs_button.setAccessibleName("Pairs")
    window.pairs_button.setObjectName("ghostButton")
    window.pairs_button.setIcon(window._asset_icon("pairs.svg"))
    window.pairs_button.setToolTip(tr("Doppelte iPhone-Paare (IMG / IMG_E) aufräumen"))
    window.pairs_button.clicked.connect(window.open_pair_cleanup)
    window.pairs_button.setEnabled(False)

    for button in (
        open_button,
        window.scan_button,
        window.run_button,
        window.run_images_button,
        window.run_videos_button,
        window.run_selected_button,
        window.export_button,
        window.missing_button,
        window.jpeg_fix_button,
        window.explorer_button,
        window.lasso_button,
        window.pairs_button,
    ):
        _style_header_button(button)

    brand_row.addWidget(logo)
    brand_row.addLayout(title_box, 1)
    action_row.addWidget(open_button)
    action_row.addWidget(window.scan_button)
    action_row.addWidget(window.run_button)
    action_row.addWidget(window.run_images_button)
    action_row.addWidget(window.run_videos_button)
    action_row.addWidget(window.run_selected_button)
    action_row.addWidget(window.export_button)
    action_row.addWidget(window.missing_button)
    action_row.addWidget(window.jpeg_fix_button)
    action_row.addWidget(window.explorer_button)
    action_row.addWidget(window.lasso_button)
    action_row.addWidget(window.pairs_button)
    action_row.addStretch(1)
    header_layout.addLayout(brand_row)
    header_layout.addLayout(action_row)
    return header
