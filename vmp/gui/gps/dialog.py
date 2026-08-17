"""Unified automatic/manual review dialog for media without GPS positions."""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

from PyQt6.QtCore import (
    QBuffer,
    QByteArray,
    QIODevice,
    QPoint,
    QSize,
    Qt,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import QCloseEvent, QDesktopServices, QIcon, QImage
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.i18n import tr
from ...core.models import GpsRepairSettings, MapSettings
from ...gps_repair import (
    GpsAssignment,
    GpsRepairRecord,
    GpsRepairReport,
    GpsSuggestion,
    GpsSuggestionMethod,
    GpsSuggestionStatus,
    build_gps_suggestions,
    surrounding_anchors,
)
from ...map_providers import leaflet_provider_script
from ...metadata import gps_coordinates
from ..common.map_provider import MapProviderCombo, apply_map_provider
from ..common.theme import asset_path
from ..common.thumbnails import ThumbnailService, ThumbRelay
from ..lasso.map_view import qwebchannel_js, webengine_available
from .map_view import MAP_HTML, GpsMapBridge, configure_local_map_settings

LOGGER = logging.getLogger("vmp.gui.gps.dialog")
_RELATIVE_GEOMETRY_PREFIX = "relative-v1:"


class GpsRepairDialog(QDialog):
    """Review suggestions, place manual pins, and request a standalone write batch."""

    applyRequested = pyqtSignal(object, bool)

    def __init__(
        self,
        parent: QWidget,
        records: list[GpsRepairRecord],
        settings: GpsRepairSettings,
        *,
        geometry: str = "",
        ffmpeg: str = "ffmpeg",
        thumbnail_workers: int = 4,
        thumbnail_cache_mode: str = "ram",
        map_settings: MapSettings | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Fehlende GPS-Positionen ergänzen"))
        self.setMinimumSize(900, 600)
        self.resize(1180, 760)
        self._records = records
        self._settings = settings
        self._map_settings = map_settings if map_settings is not None else MapSettings()
        self._suggestions: dict[Path, GpsSuggestion] = {}
        self._items: dict[Path, QTreeWidgetItem] = {}
        self._checked: set[Path] = set()
        self._auto_checked: set[Path] = set()
        self._manual: dict[Path, GpsAssignment] = {}
        self._errors: dict[Path, str] = {}
        self._draft_pin: tuple[float, float] | None = None
        self._map_ready = False
        self._map_html_path: Path | None = None
        self._map_thumbnail_paths: set[Path] = set()
        self._map_thumbnail_requested: set[Path] = set()
        self._updating_tree = False
        self._busy = False
        self._cleaned = False

        self._relay = ThumbRelay()
        self._relay.ready.connect(self._thumbnail_ready)
        self._thumbs = ThumbnailService(
            ffmpeg,
            self._relay,
            size=112,
            workers=max(0, thumbnail_workers),
            cache_mode=thumbnail_cache_mode,
        )
        self._build_ui()
        self._recalculate()
        if geometry:
            self._restore_dialog_geometry(geometry)

    def result_geometry(self) -> str:
        """Return size/state plus a position relative to the parent's top-left corner."""
        qt_geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
        parent = self.parentWidget()
        if parent is None:
            return qt_geometry
        if self.isMaximized():
            top_left = self.normalGeometry().topLeft()
        else:
            top_left = self.frameGeometry().topLeft()
        offset = top_left - parent.frameGeometry().topLeft()
        return f"{_RELATIVE_GEOMETRY_PREFIX}{offset.x()}:{offset.y()}:{qt_geometry}"

    def _restore_dialog_geometry(self, saved: str) -> None:
        """Restore old absolute blobs or the parent-relative GPS dialog format."""
        try:
            if not saved.startswith(_RELATIVE_GEOMETRY_PREFIX):
                self.restoreGeometry(QByteArray.fromBase64(saved.encode("ascii")))
                return
            payload = saved[len(_RELATIVE_GEOMETRY_PREFIX) :]
            offset_x, offset_y, qt_geometry = payload.split(":", 2)
            restored = self.restoreGeometry(QByteArray.fromBase64(qt_geometry.encode("ascii")))
            parent = self.parentWidget()
            if not restored or parent is None:
                return
            target = parent.frameGeometry().topLeft() + QPoint(int(offset_x), int(offset_y))
            maximized = self.isMaximized()
            if maximized:
                self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMaximized)
            self.move(target)
            if maximized:
                self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)
        except (TypeError, ValueError):
            LOGGER.debug("Could not restore GPS repair window geometry", exc_info=True)

    @property
    def remaining_missing(self) -> int:
        return sum(not record.has_gps for record in self._records)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        title = QLabel(tr("GPS-Lücken prüfen und ergänzen"))
        title.setObjectName("sectionTitle")
        hint = QLabel(
            tr(
                "Automatische Positionen sind Vorschläge. Wähle sichere Fälle gesammelt aus "
                "oder setze den Pin für eine oder mehrere Dateien manuell."
            )
        )
        hint.setWordWrap(True)
        hint.setObjectName("sectionHint")
        outer.addWidget(title)
        outer.addWidget(hint)

        toolbar = QHBoxLayout()
        self.filter_combo = QComboBox()
        self.filter_combo.currentIndexChanged.connect(self._apply_filter)
        toolbar.addWidget(self.filter_combo)
        self.safe_button = QPushButton(tr("Sichere auswählen"))
        self.safe_button.clicked.connect(self._select_safe)
        toolbar.addWidget(self.safe_button)
        clear_button = QPushButton(tr("Auswahl aufheben"))
        clear_button.clicked.connect(self._clear_checks)
        toolbar.addWidget(clear_button)
        toolbar.addStretch(1)
        self.selection_label = QLabel()
        toolbar.addWidget(self.selection_label)
        outer.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_list_panel())
        splitter.addWidget(self._build_map_panel())
        splitter.setSizes([470, 710])
        outer.addWidget(splitter, 1)

        advanced_toggle = QToolButton()
        advanced_toggle.setText(tr("▶ Automatik-Grenzen"))
        advanced_toggle.setCheckable(True)
        advanced_toggle.toggled.connect(
            lambda checked: (
                self.advanced_panel.setVisible(checked),
                advanced_toggle.setText(tr("▼ Automatik-Grenzen") if checked else tr("▶ Automatik-Grenzen")),
            )
        )
        outer.addWidget(advanced_toggle)
        self.advanced_panel = self._build_advanced_panel()
        self.advanced_panel.setVisible(False)
        outer.addWidget(self.advanced_panel)

        footer = QHBoxLayout()
        self.status_label = QLabel(tr("Noch nichts ausgewählt."))
        footer.addWidget(self.status_label, 1)
        self.apply_button = QPushButton(tr("Ausgewählte GPS-Daten schreiben"))
        self.apply_button.setObjectName("primaryButton")
        self.apply_button.clicked.connect(self._request_apply)
        footer.addWidget(self.apply_button)
        close_button = QPushButton(tr("Schließen"))
        close_button.clicked.connect(self.reject)
        footer.addWidget(close_button)
        outer.addLayout(footer)

    def _build_list_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("sideSection")
        layout = QVBoxLayout(panel)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("Datei"), tr("Zeit"), tr("Bewertung")])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setIconSize(QSize(72, 54))
        self.tree.setAlternatingRowColors(True)
        check_icon = asset_path("check.svg").as_posix()
        self.tree.setStyleSheet(
            "QTreeWidget::indicator { width: 18px; height: 18px; border: 1px solid #718096;"
            " border-radius: 4px; background: #ffffff; }"
            "QTreeWidget::indicator:unchecked:hover { border: 2px solid #2f6fed; }"
            "QTreeWidget::indicator:checked { background: #2f6fed; border-color: #2f6fed;"
            f' image: url("{check_icon}"); }}'
            "QTreeWidget::indicator:checked:hover { background: #245fd4; border-color: #245fd4; }"
        )
        self.tree.setColumnWidth(0, 240)
        self.tree.setColumnWidth(1, 125)
        self.tree.itemSelectionChanged.connect(self._active_changed)
        self.tree.itemChanged.connect(self._item_changed)
        layout.addWidget(self.tree, 1)
        self.detail_toggle = QToolButton()
        self.detail_toggle.setText(tr("▶ Details"))
        self.detail_toggle.setCheckable(True)
        self.detail_toggle.toggled.connect(self._toggle_details)
        layout.addWidget(self.detail_toggle)
        self.detail_label = QLabel(tr("Datei auswählen, um Anker und Begründung zu sehen."))
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail_label.setVisible(False)
        layout.addWidget(self.detail_label)
        return panel

    def _toggle_details(self, expanded: bool) -> None:
        self.detail_label.setVisible(expanded)
        self.detail_toggle.setText(tr("▼ Details") if expanded else tr("▶ Details"))

    def _build_map_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("sideSection")
        layout = QVBoxLayout(panel)
        self.map_status = QLabel(tr("Karte wird geladen …"))
        self.map_status.setObjectName("sectionHint")
        map_header = QHBoxLayout()
        map_header.addWidget(self.map_status, 1)
        map_header.addWidget(QLabel(tr("Kartenanbieter:")))
        self.map_provider_combo = MapProviderCombo(self._map_settings)
        self.map_provider_combo.setEnabled(False)
        map_header.addWidget(self.map_provider_combo)
        layout.addLayout(map_header)
        self.map_legend = QLabel(
            '<span style="color:#e0532f">●</span> '
            + tr("Für Vorschlag verwendeter GPS-Anker")
            + ' &nbsp;&nbsp; <span style="color:#2f6fed">●</span> '
            + tr("Zusätzliches zeitliches Umfeld")
            + " &nbsp;&nbsp; 📍 "
            + tr("Zielposition des ausgewählten Bildes")
        )
        self.map_legend.setWordWrap(True)
        self.map_legend.setObjectName("sectionHint")
        layout.addWidget(self.map_legend)
        self.map_host = QWidget()
        map_layout = QVBoxLayout(self.map_host)
        map_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.map_host, 1)

        self._bridge = GpsMapBridge(self)
        self._bridge.pinMoved.connect(self._pin_moved)
        self._bridge.statusChanged.connect(self._map_status_changed)
        if webengine_available():
            from PyQt6.QtWebChannel import QWebChannel
            from PyQt6.QtWebEngineCore import QWebEnginePage
            from PyQt6.QtWebEngineWidgets import QWebEngineView

            class _GpsLoggingPage(QWebEnginePage):
                def javaScriptConsoleMessage(self, level, message, line, source):
                    LOGGER.info("GPS map JS [%s] %s:%s — %s", level, source, line, message)

            self.view = QWebEngineView()
            self.map_provider_combo.set_changed_callback(
                lambda: apply_map_provider(self.view, self._map_settings)
            )
            page = _GpsLoggingPage(self.view)
            self.view.setPage(page)

            # The page is loaded via file://. Qt blocks its CDN scripts and map
            # tiles unless remote access is explicitly enabled.
            configure_local_map_settings(self.view.settings())

            channel = QWebChannel(page)
            channel.registerObject("gpsBridge", self._bridge)
            page.setWebChannel(channel)
            self._channel = channel
            html = MAP_HTML.replace("__QWEBCHANNEL_JS__", qwebchannel_js()).replace(
                "__MAP_PROVIDER_JS__", leaflet_provider_script(self._map_settings)
            )
            with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as handle:
                handle.write(html)
                self._map_html_path = Path(handle.name)
            self.view.loadStarted.connect(lambda: LOGGER.info("GPS map: load started"))
            self.view.loadProgress.connect(lambda percent: LOGGER.debug("GPS map: load %s%%", percent))
            self.view.loadFinished.connect(self._map_load_finished)
            url = QUrl.fromLocalFile(str(self._map_html_path))
            LOGGER.info("GPS map: loading %s", url.toString())
            self.view.load(url)
            map_layout.addWidget(self.view)
        else:
            self.view = None
            self.map_provider_combo.setEnabled(False)
            unavailable = QLabel(
                tr("Kartendarstellung ist nicht verfügbar. Auto-Vorschläge können weiterhin verwendet werden.")
            )
            unavailable.setWordWrap(True)
            unavailable.setAlignment(Qt.AlignmentFlag.AlignCenter)
            map_layout.addWidget(unavailable)

        actions = QHBoxLayout()
        self.coordinate_label = QLabel("—")
        actions.addWidget(self.coordinate_label, 1)
        self.pin_apply_button = QPushButton(tr("Position für Auswahl übernehmen"))
        self.pin_apply_button.clicked.connect(self._apply_pin_to_selection)
        self.pin_apply_button.setEnabled(False)
        actions.addWidget(self.pin_apply_button)
        layout.addLayout(actions)
        return panel

    def _spin(self, minimum: int, maximum: int, value: int, suffix: str = "") -> QSpinBox:
        widget = QSpinBox()
        widget.setRange(minimum, maximum)
        widget.setValue(value)
        widget.setSuffix(suffix)
        return widget

    def _double_spin(self, minimum: float, maximum: float, value: float, suffix: str = "") -> QDoubleSpinBox:
        widget = QDoubleSpinBox()
        widget.setRange(minimum, maximum)
        widget.setDecimals(1)
        widget.setValue(value)
        widget.setSuffix(suffix)
        return widget

    def _build_advanced_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("sideSection")
        form = QFormLayout(panel)
        s = self._settings
        self.single_safe = self._spin(1, 120, s.single_safe_minutes, " min")
        self.pair_safe = self._spin(1, 1440, s.pair_safe_minutes, " min")
        self.pair_distance = self._double_spin(0.1, 500.0, s.pair_safe_distance_km, " km")
        self.pair_speed = self._double_spin(1.0, 300.0, s.pair_safe_speed_kmh, " km/h")
        self.cluster_count = self._spin(3, 20, s.cluster_min_anchors)
        self.cluster_span = self._spin(1, 1440, s.cluster_min_span_minutes, " min")
        self.cluster_radius = self._spin(10, 5000, s.cluster_radius_m, " m")
        self.cluster_gap = self._spin(1, 1440, s.cluster_max_gap_minutes, " min")
        self.review_hours = self._spin(1, 48, s.review_pair_max_hours, " h")
        self.review_distance = self._double_spin(1.0, 2000.0, s.review_pair_max_distance_km, " km")
        self.review_single = self._spin(1, 1440, s.review_single_max_minutes, " min")
        fields = (
            (tr("Ein Anker sicher"), self.single_safe),
            (tr("Zwei Anker sicher"), self.pair_safe),
            (tr("Sichere Distanz"), self.pair_distance),
            (tr("Sichere Geschwindigkeit"), self.pair_speed),
            (tr("Cluster-Anker"), self.cluster_count),
            (tr("Cluster-Mindestdauer"), self.cluster_span),
            (tr("Cluster-Radius"), self.cluster_radius),
            (tr("Cluster-Maximallücke"), self.cluster_gap),
            (tr("Prüfen: maximale Dauer"), self.review_hours),
            (tr("Prüfen: maximale Distanz"), self.review_distance),
            (tr("Prüfen: einzelner Anker"), self.review_single),
        )
        for label, widget in fields:
            form.addRow(label, widget)
            widget.valueChanged.connect(self._threshold_changed)
        reset = QPushButton(tr("Defaults wiederherstellen"))
        reset.clicked.connect(self._reset_thresholds)
        form.addRow("", reset)
        return panel

    def _sync_settings(self) -> None:
        s = self._settings
        s.single_safe_minutes = self.single_safe.value()
        s.pair_safe_minutes = self.pair_safe.value()
        s.pair_safe_distance_km = self.pair_distance.value()
        s.pair_safe_speed_kmh = self.pair_speed.value()
        s.cluster_min_anchors = self.cluster_count.value()
        s.cluster_min_span_minutes = self.cluster_span.value()
        s.cluster_radius_m = self.cluster_radius.value()
        s.cluster_max_gap_minutes = self.cluster_gap.value()
        minimum_review_hours = (s.pair_safe_minutes + 59) // 60
        minimum_review_distance = s.pair_safe_distance_km
        minimum_review_single = s.single_safe_minutes
        for widget, minimum in (
            (self.review_hours, minimum_review_hours),
            (self.review_distance, minimum_review_distance),
            (self.review_single, minimum_review_single),
        ):
            if widget.value() < minimum:
                widget.blockSignals(True)
                widget.setValue(minimum)
                widget.blockSignals(False)
        s.review_pair_max_hours = self.review_hours.value()
        s.review_pair_max_distance_km = self.review_distance.value()
        s.review_single_max_minutes = self.review_single.value()

    def _threshold_changed(self) -> None:
        self._sync_settings()
        self._recalculate()

    def _reset_thresholds(self) -> None:
        defaults = GpsRepairSettings()
        values = (
            (self.single_safe, defaults.single_safe_minutes),
            (self.pair_safe, defaults.pair_safe_minutes),
            (self.pair_distance, defaults.pair_safe_distance_km),
            (self.pair_speed, defaults.pair_safe_speed_kmh),
            (self.cluster_count, defaults.cluster_min_anchors),
            (self.cluster_span, defaults.cluster_min_span_minutes),
            (self.cluster_radius, defaults.cluster_radius_m),
            (self.cluster_gap, defaults.cluster_max_gap_minutes),
            (self.review_hours, defaults.review_pair_max_hours),
            (self.review_distance, defaults.review_pair_max_distance_km),
            (self.review_single, defaults.review_single_max_minutes),
        )
        for widget, value in values:
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)
        self._threshold_changed()

    def _recalculate(self) -> None:
        suggestions = build_gps_suggestions(self._records, self._settings)
        self._suggestions = {suggestion.target.path: suggestion for suggestion in suggestions}
        valid_paths = set(self._suggestions)
        newly_safe = {
            path
            for path, suggestion in self._suggestions.items()
            if suggestion.status == GpsSuggestionStatus.SAFE and suggestion.has_position
        }
        no_longer_safe = self._auto_checked - newly_safe
        self._checked.difference_update(no_longer_safe)
        self._auto_checked.intersection_update(newly_safe)
        self._checked.intersection_update(valid_paths)
        self._manual = {path: value for path, value in self._manual.items() if path in valid_paths}
        self._populate_tree(suggestions)

    def _populate_tree(self, suggestions: list[GpsSuggestion]) -> None:
        current_path = self._active_path()
        self._updating_tree = True
        self.tree.clear()
        self._items.clear()
        try:
            for suggestion in suggestions:
                target = suggestion.target
                time_text = target.local_dt.strftime("%Y-%m-%d %H:%M:%S") if target.local_dt else tr("ohne Zeit")
                status_text = {
                    GpsSuggestionStatus.SAFE: tr("Sicher"),
                    GpsSuggestionStatus.REVIEW: tr("Prüfen"),
                    GpsSuggestionStatus.MANUAL: tr("Manuell"),
                }[suggestion.status]
                if target.path in self._manual:
                    status_text = tr("Manuell gesetzt")
                item = QTreeWidgetItem([target.path.name, time_text, status_text])
                item.setData(0, Qt.ItemDataRole.UserRole, str(target.path))
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Checked if target.path in self._checked else Qt.CheckState.Unchecked)
                item.setToolTip(2, suggestion.summary)
                self.tree.addTopLevelItem(item)
                self._items[target.path] = item
                cached = self._thumbs.cached(target.path, target.kind.value)
                if isinstance(cached, QImage) and not cached.isNull():
                    item.setIcon(0, QIcon.fromTheme("image-x-generic", QIcon()))
                    item.setIcon(0, QIcon(self._pixmap(cached)))
                self._thumbs.submit(target.path, target.kind.value, 1)
        finally:
            self._updating_tree = False
        self._rebuild_filter()
        self._update_selection_state()
        if current_path in self._items:
            self.tree.setCurrentItem(self._items[current_path])
        elif self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        else:
            self._draft_pin = None
            self.coordinate_label.setText("—")
            self.pin_apply_button.setEnabled(False)
            self.detail_label.setText(tr("Alle gescannten Dateien enthalten GPS-Positionen."))

    @staticmethod
    def _pixmap(image: QImage):
        from PyQt6.QtGui import QPixmap

        return QPixmap.fromImage(image)

    @staticmethod
    def _thumbnail_data_url(image: QImage) -> str:
        """Encode a small cached image for an in-page map tooltip."""
        buffer = QBuffer()
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
            return ""
        try:
            if not image.save(buffer, "PNG"):
                return ""
            encoded = bytes(buffer.data().toBase64()).decode("ascii")
            return f"data:image/png;base64,{encoded}"
        finally:
            buffer.close()

    def _thumbnail_ready(self, path_text: str, image: QImage | None, _token: int) -> None:
        path = Path(path_text)
        item = self._items.get(path)
        if item is not None and isinstance(image, QImage) and not image.isNull():
            item.setIcon(0, QIcon(self._pixmap(image)))
        if path in self._map_thumbnail_paths and isinstance(image, QImage) and not image.isNull():
            active = self._active_path()
            if active is not None and active in self._suggestions:
                suggestion = self._suggestions[active]
                assignment = self._manual.get(active)
                pin = (assignment.lat, assignment.lon) if assignment is not None else (
                    (suggestion.lat, suggestion.lon) if suggestion.has_position else None
                )
                self._show_map(suggestion, pin)

    def _rebuild_filter(self) -> None:
        current = self.filter_combo.currentData()
        counts = {status.value: 0 for status in GpsSuggestionStatus}
        for suggestion in self._suggestions.values():
            counts[suggestion.status.value] += 1
        self.filter_combo.blockSignals(True)
        self.filter_combo.clear()
        self.filter_combo.addItem(tr("Alle ({count})").format(count=len(self._suggestions)), "all")
        self.filter_combo.addItem(tr("Sicher ({count})").format(count=counts["safe"]), "safe")
        self.filter_combo.addItem(tr("Prüfen ({count})").format(count=counts["review"]), "review")
        self.filter_combo.addItem(tr("Manuell ({count})").format(count=counts["manual"]), "manual")
        index = self.filter_combo.findData(current)
        self.filter_combo.setCurrentIndex(max(0, index))
        self.filter_combo.blockSignals(False)
        self._apply_filter()

    def _apply_filter(self) -> None:
        wanted = self.filter_combo.currentData() or "all"
        for path, item in self._items.items():
            suggestion = self._suggestions[path]
            item.setHidden(wanted != "all" and suggestion.status.value != wanted)

    def _active_path(self) -> Path | None:
        item = self.tree.currentItem()
        return Path(item.data(0, Qt.ItemDataRole.UserRole)) if item is not None else None

    def _selected_paths(self) -> list[Path]:
        paths = [Path(item.data(0, Qt.ItemDataRole.UserRole)) for item in self.tree.selectedItems()]
        active = self._active_path()
        return paths or ([active] if active is not None else [])

    def _active_changed(self) -> None:
        # Qt re-emits selection changes while _populate_tree() clears the old
        # items; the rebuilt suggestion dict no longer covers those paths, and
        # an exception escaping this Qt-invoked slot would abort the process.
        if self._updating_tree:
            return
        path = self._active_path()
        if path is None:
            return
        suggestion = self._suggestions.get(path)
        if suggestion is None:
            return
        details = [suggestion.summary, *suggestion.details]
        error = self._errors.get(path)
        if error:
            details.append(tr("Fehler: {error}").format(error=error))
        self.detail_label.setText("\n".join(details))
        assignment = self._manual.get(path)
        pin = (assignment.lat, assignment.lon) if assignment is not None else (
            (suggestion.lat, suggestion.lon) if suggestion.has_position else None
        )
        self._draft_pin = pin
        self._show_map(suggestion, pin)

    def _show_map(self, suggestion: GpsSuggestion, pin: tuple[float, float] | None) -> None:
        context = surrounding_anchors(self._records, suggestion.target, 2)
        immediate = set(suggestion.anchor_paths)
        context_paths = {record.path for record in context}
        records_by_path = {record.path: record for record in self._records}
        # Device-specific interpolation anchors are not necessarily among the
        # nearest global records. They must nevertheless always be visible.
        context.extend(
            records_by_path[path]
            for path in suggestion.anchor_paths
            if path not in context_paths and path in records_by_path
        )
        context.sort(
            key=lambda record: (
                record.local_dt is None,
                record.local_dt.isoformat() if record.local_dt else "",
            )
        )
        thumbnail_records = [*context, suggestion.target]
        thumbnail_paths = {record.path for record in thumbnail_records}
        if thumbnail_paths != self._map_thumbnail_paths:
            self._map_thumbnail_paths = thumbnail_paths
            self._map_thumbnail_requested.clear()
        thumbnails: dict[Path, str] = {}
        for record in thumbnail_records:
            cached = self._thumbs.cached(record.path, record.kind.value)
            if isinstance(cached, QImage) and not cached.isNull():
                thumbnails[record.path] = self._thumbnail_data_url(cached)
            elif record.path not in self._map_thumbnail_requested:
                self._map_thumbnail_requested.add(record.path)
                self._thumbs.submit(record.path, record.kind.value, 1)

        anchor_roles = {
            path: tr("Verwendeter GPS-Anker") for path in suggestion.anchor_paths
        }
        if suggestion.anchor_paths:
            anchor_roles[suggestion.anchor_paths[0]] = tr("Verwendeter GPS-Anker · vorher")
        if len(suggestion.anchor_paths) > 1:
            anchor_roles[suggestion.anchor_paths[-1]] = tr("Verwendeter GPS-Anker · nachher")
        payload = {
            "context": [
                {
                    "lat": record.lat,
                    "lon": record.lon,
                    "label": f"{record.path.name} · {record.local_dt.strftime('%H:%M:%S') if record.local_dt else ''}",
                    "immediate": record.path in immediate,
                    "role": anchor_roles.get(record.path, tr("Zusätzliches zeitliches Umfeld")),
                    "thumbnail": thumbnails.get(record.path, ""),
                }
                for record in context
                if record.lat is not None and record.lon is not None
            ],
            "pin": {
                "lat": pin[0],
                "lon": pin[1],
                "label": suggestion.target.path.name,
                "role": (
                    tr("Manuell gesetzte Zielposition")
                    if suggestion.target.path in self._manual
                    else tr("Vorgeschlagene Zielposition")
                ),
                "thumbnail": thumbnails.get(suggestion.target.path, ""),
            }
            if pin is not None
            else None,
        }
        self._bridge.set_payload(payload)
        if pin is not None:
            self.coordinate_label.setText(f"{pin[0]:.6f}, {pin[1]:.6f}")
        else:
            self.coordinate_label.setText("—")
        if self.view is not None and self._map_ready:
            self.view.page().runJavaScript(f"render({json.dumps(self._bridge.get_payload())});")
        self.pin_apply_button.setEnabled(self._map_ready and pin is not None and not self._busy)

    def _map_load_finished(self, ok: bool) -> None:
        if ok:
            LOGGER.info("GPS map: page load finished OK")
        else:
            LOGGER.warning("GPS map: page load failed")
            self._map_status_changed("page_load_failed")

    def _map_status_changed(self, status: str) -> None:
        LOGGER.info("GPS map: status=%s", status)
        self._map_ready = status == "ready"
        self.map_provider_combo.setEnabled(self._map_ready)
        messages = {
            "ready": tr("Karte bereit – klicken oder Pin verschieben."),
            "leaflet_missing": tr("Kartenbibliothek konnte nicht geladen werden; Auto-Vorschläge bleiben nutzbar."),
            "javascript_error": tr("Kartenfehler; Auto-Vorschläge bleiben nutzbar."),
            "page_load_failed": tr("Kartenseite konnte nicht geladen werden; Auto-Vorschläge bleiben nutzbar."),
        }
        self.map_status.setText(messages.get(status, tr("Karte offline; Auto-Vorschläge bleiben nutzbar.")))
        self.pin_apply_button.setEnabled(self._map_ready and self._draft_pin is not None and not self._busy)

    def _pin_moved(self, latitude: float, longitude: float) -> None:
        self._draft_pin = (latitude, longitude)
        self.coordinate_label.setText(f"{latitude:.6f}, {longitude:.6f}")
        self.pin_apply_button.setEnabled(not self._busy)

    def _apply_pin_to_selection(self) -> None:
        if self._draft_pin is None:
            return
        paths = self._selected_paths()
        if not paths:
            return
        latitude, longitude = self._draft_pin
        self._updating_tree = True
        try:
            for path in paths:
                suggestion = self._suggestions[path]
                self._manual[path] = GpsAssignment(
                    target=suggestion.target,
                    lat=latitude,
                    lon=longitude,
                    method=GpsSuggestionMethod.MANUAL,
                    anchor_paths=suggestion.anchor_paths,
                )
                self._checked.add(path)
                self._auto_checked.discard(path)
                item = self._items[path]
                item.setCheckState(0, Qt.CheckState.Checked)
                item.setText(2, tr("Manuell gesetzt"))
        finally:
            self._updating_tree = False
        self._update_selection_state()
        self._advance_after_manual_assignment(paths)

    def _advance_after_manual_assignment(self, edited_paths: list[Path]) -> None:
        """Select the next visible item that has not received a manual position."""
        edited_rows = [
            self.tree.indexOfTopLevelItem(self._items[path])
            for path in edited_paths
            if path in self._items
        ]
        start_row = max(edited_rows, default=-1) + 1
        next_item: QTreeWidgetItem | None = None
        for row in range(start_row, self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(row)
            path = Path(item.data(0, Qt.ItemDataRole.UserRole))
            if not item.isHidden() and path not in self._manual:
                next_item = item
                break
        if next_item is None:
            return

        signals_were_blocked = self.tree.blockSignals(True)
        try:
            self.tree.clearSelection()
            self.tree.setCurrentItem(next_item)
            next_item.setSelected(True)
            self.tree.scrollToItem(next_item)
        finally:
            self.tree.blockSignals(signals_were_blocked)
        self._active_changed()

    def _item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        if self._updating_tree:
            return
        path = Path(item.data(0, Qt.ItemDataRole.UserRole))
        if item.checkState(0) == Qt.CheckState.Checked:
            self._checked.add(path)
        else:
            self._checked.discard(path)
            self._auto_checked.discard(path)
        self._update_selection_state()

    def _select_safe(self) -> None:
        self._updating_tree = True
        try:
            for path, suggestion in self._suggestions.items():
                if suggestion.status == GpsSuggestionStatus.SAFE and suggestion.has_position:
                    self._checked.add(path)
                    self._auto_checked.add(path)
                    self._items[path].setCheckState(0, Qt.CheckState.Checked)
        finally:
            self._updating_tree = False
        self._update_selection_state()

    def _clear_checks(self) -> None:
        self._checked.clear()
        self._auto_checked.clear()
        self._updating_tree = True
        try:
            for item in self._items.values():
                item.setCheckState(0, Qt.CheckState.Unchecked)
        finally:
            self._updating_tree = False
        self._update_selection_state()

    def _update_selection_state(self) -> None:
        count = len(self._checked)
        self.selection_label.setText(tr("{count} ausgewählt").format(count=count))
        self.apply_button.setEnabled(count > 0 and not self._busy)

    def _assignments(self) -> tuple[list[GpsAssignment], list[Path]]:
        assignments: list[GpsAssignment] = []
        missing: list[Path] = []
        for path in sorted(self._checked, key=lambda value: str(value).casefold()):
            manual = self._manual.get(path)
            if manual is not None:
                assignments.append(manual)
                continue
            suggestion = self._suggestions[path]
            if not suggestion.has_position:
                missing.append(path)
                continue
            assignments.append(
                GpsAssignment(
                    target=suggestion.target,
                    lat=suggestion.lat,  # type: ignore[arg-type]
                    lon=suggestion.lon,  # type: ignore[arg-type]
                    method=suggestion.method,
                    anchor_paths=suggestion.anchor_paths,
                )
            )
        return assignments, missing

    def _request_apply(self) -> None:
        assignments, missing = self._assignments()
        if missing:
            QMessageBox.warning(
                self,
                tr("GPS ergänzen"),
                tr("{count} ausgewählte Datei(en) haben noch keine Position.").format(count=len(missing)),
            )
            return
        if not assignments:
            return
        answer = QMessageBox.question(
            self,
            tr("GPS-Backup"),
            tr(
                "Vor dem Schreiben ein versioniertes Backup der {count} Datei(en) erstellen?\n\n"
                "Ja = Backup erstellen\nNein = ohne Backup fortfahren"
            ).format(count=len(assignments)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return
        self.set_busy(True)
        self.applyRequested.emit(assignments, answer == QMessageBox.StandardButton.Yes)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.tree.setEnabled(not busy)
        self.safe_button.setEnabled(not busy)
        self.apply_button.setEnabled(not busy and bool(self._checked))
        self.pin_apply_button.setEnabled(not busy and self._map_ready and self._draft_pin is not None)
        self.status_label.setText(tr("GPS-Daten werden geschrieben …") if busy else tr("Bereit."))

    def apply_report(self, report: GpsRepairReport) -> None:
        self.set_busy(False)
        for entry in report.entries:
            path = entry.assignment.target.path
            coords = gps_coordinates(entry.readback.tags) if entry.readback is not None else None
            if coords is not None:
                record = next((candidate for candidate in self._records if candidate.path == path), None)
                if record is not None:
                    record.lat, record.lon = coords
                    record.metadata = entry.readback
                self._checked.discard(path)
                self._auto_checked.discard(path)
                self._manual.pop(path, None)
                self._errors.pop(path, None)
            elif entry.error:
                self._errors[path] = entry.error
        self._recalculate()
        summary = tr("{changed} GPS-Position(en) geschrieben, {failed} Fehler.").format(
            changed=report.changed, failed=report.failed
        )
        self.status_label.setText(summary)
        message = QMessageBox(self)
        message.setWindowTitle(tr("GPS ergänzen"))
        message.setText(summary)
        message.setIcon(QMessageBox.Icon.Information)
        message.setStandardButtons(
            QMessageBox.StandardButton.Ok
            | (QMessageBox.StandardButton.Open if report.manifest_paths else QMessageBox.StandardButton.NoButton)
        )
        if report.manifest_paths:
            message.setInformativeText(
                tr("Manifest: {path}\nMit „Öffnen“ den Ordner anzeigen.").format(path=report.manifest_paths[0])
            )
        if message.exec() == QMessageBox.StandardButton.Open:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(report.manifest_paths[0].parent)))

    def reject(self) -> None:
        if self._busy:
            QMessageBox.information(self, tr("GPS ergänzen"), tr("Bitte warte, bis der laufende Schreibvorgang beendet ist."))
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._busy:
            event.ignore()
            return
        self._cleanup()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        if self._busy:
            return
        self._cleanup()
        super().done(result)

    def _cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        self._thumbs.stop()
        if self._map_html_path is not None:
            try:
                self._map_html_path.unlink(missing_ok=True)
            except OSError:
                pass
