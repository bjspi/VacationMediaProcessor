"""Widget construction for the Trip Lasso dialog (map, strip, side panel, footer)."""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import QSize, Qt, QUrl
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QRadioButton,
    QSlider,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.i18n import tr
from .histogram import DayHistogramWidget
from .map_view import MAP_HTML, MapBridge, qwebchannel_js
from ..common.theme import asset_path
from .thumb_strip import THUMB_MAX, THUMB_MIN, ThumbDelegate, ThumbStrip

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .dialog import LassoDialog

LOGGER = logging.getLogger("vmp.gui.lasso.ui")

def build_lasso_ui(dialog: "LassoDialog") -> None:
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(14, 14, 14, 14)
    layout.setSpacing(10)

    # Mode toggle
    mode_row = QHBoxLayout()
    dialog.map_mode_radio = QRadioButton(tr("Karte (Polygon)"))
    dialog.date_mode_radio = QRadioButton(tr("Datum"))
    dialog.histogram_mode_radio = QRadioButton(tr("Histogramm"))
    dialog.map_mode_radio.setChecked(True)
    dialog._mode_group = QButtonGroup(dialog)
    dialog._mode_group.addButton(dialog.map_mode_radio, 0)
    dialog._mode_group.addButton(dialog.date_mode_radio, 1)
    dialog._mode_group.addButton(dialog.histogram_mode_radio, 2)
    dialog._mode_group.idToggled.connect(dialog._on_mode_changed)
    mode_row.addWidget(dialog.map_mode_radio)
    mode_row.addWidget(dialog.date_mode_radio)
    mode_row.addWidget(dialog.histogram_mode_radio)
    mode_row.addStretch(1)
    dialog.count_label = QLabel(tr("Noch keine Auswahl."))
    dialog.count_label.setStyleSheet("color:#475569;")
    mode_row.addWidget(dialog.count_label)
    layout.addLayout(mode_row)

    # Map + side controls, wrapped in a collapsible, sidebar-style section
    # (▾ header toggle inside a framed box, like the main app's settings).
    dialog._map_container = QWidget()
    map_layout = QHBoxLayout(dialog._map_container)
    map_layout.setContentsMargins(0, 0, 0, 0)
    map_layout.setSpacing(10)
    map_layout.addWidget(_build_map(dialog), 1)
    map_layout.addWidget(_build_side_controls(dialog))

    dialog._map_section = QFrame()
    dialog._map_section.setObjectName("lassoSection")
    dialog._map_section.setStyleSheet(
        "QFrame#lassoSection { border:1px solid #cfd6e3; border-radius:8px; background:#ffffff; }"
    )
    section_layout = QVBoxLayout(dialog._map_section)
    section_layout.setContentsMargins(8, 6, 8, 8)
    section_layout.setSpacing(6)
    dialog._map_toggle = QPushButton(tr("▾  Karte"))
    dialog._map_toggle.setObjectName("lassoSectionToggle")
    dialog._map_toggle.setCheckable(True)
    dialog._map_toggle.setChecked(True)
    dialog._map_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
    dialog._map_toggle.setToolTip(tr("Karte ein-/ausklappen, um mehr Thumbnails zu sehen"))
    dialog._map_toggle.setStyleSheet(
        "QPushButton#lassoSectionToggle { text-align:left; border:none; background:transparent;"
        " color:#111827; font-size:14px; font-weight:800; padding:0 0 2px 0; }"
        "QPushButton#lassoSectionToggle:hover { color:#1d6fe0; }"
    )
    dialog._map_toggle.toggled.connect(dialog._toggle_map)
    section_layout.addWidget(dialog._map_toggle)
    section_layout.addWidget(dialog._map_container, 1)

    strip_container = QWidget()
    strip_box = QVBoxLayout(strip_container)
    strip_box.setContentsMargins(0, 0, 0, 0)
    strip_box.setSpacing(6)
    strip_header = QHBoxLayout()
    strip_label = QLabel(tr("Auswahl (Häkchen entfernen, um auszuschließen):"))
    strip_label.setStyleSheet("font-weight:600;")
    strip_header.addWidget(strip_label)
    strip_header.addStretch(1)
    strip_header.addWidget(QLabel(tr("Größe:")))
    dialog.thumb_slider = QSlider(Qt.Orientation.Horizontal)
    dialog.thumb_slider.setRange(THUMB_MIN, THUMB_MAX)
    dialog.thumb_slider.setValue(dialog._thumb_display)
    dialog.thumb_slider.setFixedWidth(160)
    dialog.thumb_slider.setToolTip(tr("Thumbnail-Größe"))
    dialog.thumb_slider.valueChanged.connect(dialog._on_thumb_slider)
    strip_header.addWidget(dialog.thumb_slider)
    strip_box.addLayout(strip_header)

    dialog.day_histogram = DayHistogramWidget()
    dialog.day_histogram.set_records(dialog._records)
    dialog.day_histogram.selectionChanged.connect(dialog._on_histogram_days_changed)
    dialog.day_histogram.setVisible(False)
    strip_box.addWidget(dialog.day_histogram)

    dialog.strip = ThumbStrip()
    dialog.strip.setViewMode(QListWidget.ViewMode.IconMode)
    dialog.strip.setFlow(QListWidget.Flow.LeftToRight)
    dialog.strip.setWrapping(True)
    dialog.strip.setResizeMode(QListWidget.ResizeMode.Adjust)
    dialog.strip.setIconSize(QSize(dialog._thumb_display, dialog._thumb_display))
    dialog.strip.setSpacing(6)
    dialog.strip.setMinimumHeight(160)
    dialog.strip.setMovement(QListWidget.Movement.Static)
    dialog.strip.setSelectionMode(QListWidget.SelectionMode.NoSelection)
    dialog.strip.setItemDelegate(ThumbDelegate(dialog.strip))
    check_dark = asset_path("check_dark.svg").as_posix()
    dialog.strip.setStyleSheet(
        "QListWidget::indicator { width: 15px; height: 15px; border: 1px solid #8fa0b5;"
        " border-radius: 3px; background: #ffffff; }"
        f'QListWidget::indicator:checked {{ image: url("{check_dark}"); }}'
    )
    strip_box.addWidget(dialog.strip, 1)

    dialog._splitter = QSplitter(Qt.Orientation.Vertical)
    dialog._splitter.addWidget(dialog._map_section)
    dialog._splitter.addWidget(strip_container)
    dialog._splitter.setStretchFactor(0, 3)
    dialog._splitter.setStretchFactor(1, 2)
    dialog._splitter.setSizes([440, 300])
    layout.addWidget(dialog._splitter, 1)

    # Footer: destination + actions
    layout.addWidget(_build_footer(dialog))

def _build_map(dialog: "LassoDialog") -> QWidget:
    from PyQt6.QtWebChannel import QWebChannel
    from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
    from PyQt6.QtWebEngineWidgets import QWebEngineView

    class _LoggingPage(QWebEnginePage):
        """QWebEnginePage that forwards JS console output to our logger."""

        def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
            LOGGER.info("Lasso map JS [%s] %s:%s — %s", level, source, line, message)

    dialog.view = QWebEngineView()
    dialog.view.setMinimumSize(560, 380)
    page = _LoggingPage(dialog.view)
    dialog.view.setPage(page)

    # A file:// page must be allowed to pull the Leaflet CDN scripts and the
    # OSM map tiles, otherwise the map silently stays blank.
    settings = dialog.view.settings()
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)

    points = [
        {"id": str(r.path), "lat": r.lat, "lon": r.lon}
        for r in dialog._records
        if r.has_gps
    ]
    LOGGER.info("Lasso map: %s GPS points of %s records", len(points), len(dialog._records))
    dialog._bridge = MapBridge(json.dumps(points))
    dialog._bridge.polygonReceived.connect(dialog._on_polygon)
    dialog._channel = QWebChannel(page)
    dialog._channel.registerObject("bridge", dialog._bridge)
    page.setWebChannel(dialog._channel)

    qwc_js = qwebchannel_js()
    LOGGER.info("Lasso map: qwebchannel.js length=%s", len(qwc_js))
    html = MAP_HTML.replace("__QWEBCHANNEL_JS__", qwc_js)
    # Write to a temp file and load via file:// so CDN scripts and the
    # inlined channel transport behave reliably.
    tmp = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
    tmp.write(html)
    tmp.close()
    dialog._map_html_path = Path(tmp.name)

    dialog.view.loadStarted.connect(lambda: LOGGER.info("Lasso map: load started"))
    dialog.view.loadProgress.connect(lambda pct: LOGGER.debug("Lasso map: load %s%%", pct))
    dialog.view.loadFinished.connect(dialog._on_map_load_finished)

    url = QUrl.fromLocalFile(str(dialog._map_html_path))
    LOGGER.info("Lasso map: loading %s", url.toString())
    dialog.view.load(url)
    return dialog.view

def _build_side_controls(dialog: "LassoDialog") -> QWidget:
    panel = QFrame()
    panel.setFixedWidth(260)
    panel.setObjectName("lassoSide")
    outer = QVBoxLayout(panel)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(8)

    dialog._stack = QStackedWidget()

    # Map-mode hint
    map_page = QWidget()
    map_layout = QVBoxLayout(map_page)
    map_layout.setContentsMargins(0, 0, 0, 0)
    hint = QLabel(
        tr(
            "Zeichne mit dem Polygon-Werkzeug (oben links auf der Karte) eine "
            "Fläche um deine Reise. Medien ohne GPS im gleichen Zeitraum werden "
            "automatisch ergänzt."
        )
    )
    hint.setWordWrap(True)
    hint.setStyleSheet("color:#475569;")
    map_layout.addWidget(hint)
    dialog.window_label = QLabel("")
    dialog.window_label.setWordWrap(True)
    dialog.window_label.setStyleSheet("color:#0f172a; font-weight:600;")
    map_layout.addWidget(dialog.window_label)
    map_layout.addStretch(1)
    dialog._stack.addWidget(map_page)

    # Date-mode controls
    date_page = QWidget()
    date_layout = QVBoxLayout(date_page)
    date_layout.setContentsMargins(0, 0, 0, 0)
    date_layout.addWidget(QLabel(tr("Von:")))
    dialog.date_from = QDateEdit()
    dialog.date_from.setCalendarPopup(True)
    dialog.date_from.setDisplayFormat("yyyy-MM-dd")
    date_layout.addWidget(dialog.date_from)
    date_layout.addWidget(QLabel(tr("Bis:")))
    dialog.date_to = QDateEdit()
    dialog.date_to.setCalendarPopup(True)
    dialog.date_to.setDisplayFormat("yyyy-MM-dd")
    date_layout.addWidget(dialog.date_to)
    apply_button = QPushButton(tr("Anwenden"))
    apply_button.clicked.connect(dialog._apply_date_range)
    date_layout.addWidget(apply_button)
    date_layout.addStretch(1)
    dialog._stack.addWidget(date_page)

    # Histogram-mode summary
    histogram_page = QWidget()
    histogram_layout = QVBoxLayout(histogram_page)
    histogram_layout.setContentsMargins(0, 0, 0, 0)
    hist_hint = QLabel(
        tr(
            "Klicke oder ziehe über Tage mit vielen Medien. Die Auswahl unten "
            "enthält genau diese Tage."
        )
    )
    hist_hint.setWordWrap(True)
    hist_hint.setStyleSheet("color:#475569;")
    histogram_layout.addWidget(hist_hint)
    dialog.histogram_label = QLabel("")
    dialog.histogram_label.setWordWrap(True)
    dialog.histogram_label.setStyleSheet("color:#0f172a; font-weight:600;")
    histogram_layout.addWidget(dialog.histogram_label)
    histogram_layout.addStretch(1)
    dialog._stack.addWidget(histogram_page)

    outer.addWidget(dialog._stack)
    return panel

def _build_footer(dialog: "LassoDialog") -> QWidget:
    footer = QFrame()
    layout = QHBoxLayout(footer)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)

    layout.addWidget(QLabel(tr("Ziel:")))
    dialog.dest_edit = QLineEdit(str(dialog._default_root) if dialog._default_root else "")
    layout.addWidget(dialog.dest_edit, 2)
    browse = QPushButton("…")
    browse.setFixedWidth(34)
    browse.clicked.connect(dialog._choose_dest)
    layout.addWidget(browse)
    up_button = QPushButton("↑")
    up_button.setFixedWidth(34)
    up_button.setToolTip(tr("Zielverzeichnis eine Ebene höher setzen"))
    up_button.clicked.connect(dialog._dest_one_level_up)
    layout.addWidget(up_button)

    layout.addWidget(QLabel(tr("Ordner:")))
    dialog.name_edit = QLineEdit()
    dialog.name_edit.setPlaceholderText(tr("z. B. Mallorca 2024"))
    dialog.name_edit.textEdited.connect(dialog._on_name_edited)
    layout.addWidget(dialog.name_edit, 1)

    dialog.copy_checkbox = QCheckBox(tr("Kopieren statt verschieben"))
    layout.addWidget(dialog.copy_checkbox)
    dialog.load_target_checkbox = QCheckBox(tr("Nach Verschieben Zielordner laden"))
    dialog.load_target_checkbox.setChecked(dialog.load_target_after_move)
    layout.addWidget(dialog.load_target_checkbox)

    layout.addStretch(1)
    dialog.move_button = QPushButton(tr("Verschieben"))
    dialog.move_button.setObjectName("primaryButton")
    dialog.move_button.clicked.connect(dialog._on_transfer)
    dialog.cancel_button = QPushButton(tr("Abbrechen"))
    dialog.cancel_button.clicked.connect(dialog.reject)
    layout.addWidget(dialog.move_button)
    layout.addWidget(dialog.cancel_button)
    return footer

# -- helpers ----------------------------------------------------------- #
