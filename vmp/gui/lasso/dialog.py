"""Trip Lasso overlay: spatial (map polygon) + temporal (date range) media selection.

The dialog lets the user carve a "trip" out of a large folder by drawing a polygon
on a Leaflet map or by picking a date range, reviews the result as a thumbnail
strip, and moves (or copies) the chosen media into a destination folder whose name
is suggested via reverse geocoding.

The heavy lifting lives in focused sibling modules: ``thumbnails`` (decoding +
caching), ``map_view`` (Leaflet page + web channel bridge), ``histogram`` (day
selection widget), ``file_transfer`` (move/copy helpers), and ``geocode``.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime
from pathlib import Path

from PyQt6.QtCore import QByteArray, QObject, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon, QImage, QPixmap
from PyQt6.QtWidgets import QDialog, QFileDialog, QListWidgetItem, QMessageBox, QStyle, QWidget

from ...core.i18n import tr
from ..common.file_transfer import (
    parent_directory,
    perform_transfer,
    remaining_records,
    sanitize_folder_name,
    unique_target,
)
from .geocode import reverse_geocode_place
from .ui import build_lasso_ui
from .thumb_strip import THUMB_DEFAULT_SIZE, THUMB_MAX, THUMB_MIN, ThumbStrip
from .transfer_worker import TransferWorker
from .histogram import DayHistogramWidget
from ..common.thumbnails import (
    ThumbnailService,
    ThumbRelay,
    thumb_cache_key,
)
from .trip_selection import (
    TripRecord,
    TripSelection,
    centroid_of,
    default_folder_name,
    select_by_date_range,
    select_by_days,
    select_by_polygon,
)

LOGGER = logging.getLogger("vmp.gui.lasso.dialog")

_THUMB_SIZE = THUMB_DEFAULT_SIZE
_THUMB_MIN = THUMB_MIN
_THUMB_MAX = THUMB_MAX

# Module-level aliases used as test seams (tests import these names).
_DayHistogramWidget = DayHistogramWidget
_ThumbRelay = ThumbRelay
_parent_directory = parent_directory
_remaining_records = remaining_records
_thumb_cache_key = thumb_cache_key
_unique_target = unique_target
_ThumbStrip = ThumbStrip

__all__ = [
    "LassoDialog",
    "ThumbnailService",
    "perform_transfer",
]


class _NameSuggestionRelay(QObject):
    """Delivers reverse-geocoded folder name suggestions to the GUI thread."""

    suggested = pyqtSignal(str, int)  # sanitized name, selection token


class LassoDialog(QDialog):
    """Large overlay for spatial/temporal trip selection and folder move."""

    activePathsRemoved = pyqtSignal(list)

    def __init__(
        self,
        parent: QWidget | None,
        records: list[TripRecord],
        default_root: Path | None,
        ffmpeg: str | None,
        geometry: str = "",
        load_target_after_move: bool = False,
        thumbnail_cache_mode: str = "ram",
        thumbnail_workers: int = 8,
        thumbnail_display_size: int = _THUMB_SIZE,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Reise-Lasso – Medien auswählen und verschieben"))
        # Give the dialog the minimize/maximize window buttons (a plain QDialog
        # only gets a close button on Windows).
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )
        self.resize(1180, 760)
        self.setModal(True)

        self._records = records
        self._ffmpeg = ffmpeg
        self._default_root = default_root
        self._last_polygon: list[tuple[float, float]] | None = None
        self._histogram_selected_days: set[date] = set()
        self._selection = TripSelection()
        self._name_user_edited = False
        self._items_by_path: dict[str, QListWidgetItem] = {}
        self._thumb_display = max(_THUMB_MIN, min(_THUMB_MAX, int(thumbnail_display_size)))
        self._thumb_token = 0
        self._transfer_thread: QThread | None = None
        self._transfer_worker: TransferWorker | None = None
        self.load_target_after_move = load_target_after_move
        self.thumbnail_cache_mode = thumbnail_cache_mode if thumbnail_cache_mode in {"ram", "disk", "off"} else "ram"
        self.thumbnail_workers = max(1, min(12, int(thumbnail_workers)))

        # Public results read by the caller after exec().
        self.moved_sources: list[Path] = []
        self.copied = False
        self.target_dir: Path | None = None
        self.load_target_after_move_requested = False

        self._relay = ThumbRelay()
        self._relay.ready.connect(self._on_thumb_ready)
        self._name_relay = _NameSuggestionRelay()
        self._name_relay.suggested.connect(self._on_name_suggested)
        self._thumbs = ThumbnailService(
            ffmpeg,
            self._relay,
            cache_mode=self.thumbnail_cache_mode,
            workers=self.thumbnail_workers,
        )
        # Debounces: geocode lookups settle after the selection stops changing;
        # the icon-size loop over all items runs once per slider gesture.
        self._geocode_timer = QTimer(self)
        self._geocode_timer.setSingleShot(True)
        self._geocode_timer.setInterval(400)
        self._geocode_timer.timeout.connect(self._start_geocode_lookup)
        self._pending_geocode: tuple[float, float, int | None] | None = None
        self._thumb_resize_timer = QTimer(self)
        self._thumb_resize_timer.setSingleShot(True)
        self._thumb_resize_timer.setInterval(120)
        self._thumb_resize_timer.timeout.connect(self._apply_thumb_size_to_items)

        self._build_ui()
        self._prefill_dates()
        self._update_strip(TripSelection())  # empty until the user acts
        self.finished.connect(self._cleanup)
        if geometry:
            try:
                self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
            except Exception:  # noqa: BLE001
                LOGGER.debug("Could not restore lasso window geometry", exc_info=True)

    def _build_ui(self) -> None:
        """Build the dialog widgets (implementation lives in ``lasso_ui``)."""
        build_lasso_ui(self)

    def result_geometry(self) -> str:
        """Return the current window geometry (size/pos/maximized) as base64."""
        return bytes(self.saveGeometry().toBase64()).decode("ascii")

    def thumbnail_display_size(self) -> int:
        """Return the current thumbnail display size selected by the slider."""
        return self._thumb_display

    # -- UI construction --------------------------------------------------- #

    def _toggle_map(self, expanded: bool) -> None:
        """Collapse/expand the framed map section so the strip gets the height."""
        self._map_container.setVisible(expanded)
        self._map_toggle.setText(tr("▾  Karte") if expanded else tr("▸  Karte  (eingeklappt)"))
        if expanded:
            self._splitter.setSizes([440, 300])
        else:
            self._splitter.setSizes([self._map_toggle.sizeHint().height() + 24, 900])

    def _on_thumb_slider(self, value: int) -> None:
        """Resize the thumbnail icons live; the O(n) item loop is debounced."""
        self._thumb_display = value
        self.strip.setIconSize(QSize(value, value))
        self._thumb_resize_timer.start()

    def _apply_thumb_size_to_items(self) -> None:
        value = self._thumb_display
        for index in range(self.strip.count()):
            self.strip.item(index).setSizeHint(QSize(value + 20, value + 34))

    def _on_map_load_finished(self, ok: bool) -> None:
        """Log the outcome of the map page load."""
        if ok:
            LOGGER.info("Lasso map: load finished OK")
        else:
            LOGGER.warning(
                "Lasso map: load FAILED — check internet access to unpkg.com/tile.openstreetmap.org"
            )

    def _prefill_dates(self) -> None:
        from PyQt6.QtCore import QDate

        moments = [r.local_dt for r in self._records if r.local_dt is not None]
        if moments:
            lo, hi = min(moments).date(), max(moments).date()
        else:
            today = datetime.now().date()
            lo = hi = today
        self.date_from.setDate(QDate(lo.year, lo.month, lo.day))
        self.date_to.setDate(QDate(hi.year, hi.month, hi.day))

    def _on_mode_changed(self, _id: int, checked: bool) -> None:
        if not checked:
            return
        if self.map_mode_radio.isChecked():
            self._stack.setCurrentIndex(0)
            self.day_histogram.setVisible(False)
            self._run_javascript("resetColors();")
            if self._last_polygon:
                self._recompute_polygon()
            else:
                self._update_strip(TripSelection())
        elif self.date_mode_radio.isChecked():
            self._stack.setCurrentIndex(1)
            self.day_histogram.setVisible(False)
            self._apply_date_range()
        else:
            self._stack.setCurrentIndex(2)
            self.day_histogram.setVisible(True)
            self._apply_histogram_days()

    def _run_javascript(self, code: str) -> None:
        try:
            self.view.page().runJavaScript(code)
        except Exception:  # noqa: BLE001
            LOGGER.debug("runJavaScript failed: %s", code, exc_info=True)

    def _highlight_selection_on_map(self, selection: TripSelection) -> None:
        included_ids = [str(r.path) for r in selection.included if r.has_gps]
        self._run_javascript(f"highlight({json.dumps(included_ids)});")

    def _on_polygon(self, geojson: str) -> None:
        try:
            raw = json.loads(geojson)
        except json.JSONDecodeError:
            raw = []
        self._last_polygon = [(float(lat), float(lon)) for lat, lon in raw] if raw else None
        if self.map_mode_radio.isChecked():
            self._recompute_polygon()

    def _recompute_polygon(self) -> None:
        if not self._last_polygon:
            self._update_strip(TripSelection())
            return
        selection = select_by_polygon(self._records, self._last_polygon)
        self._update_strip(selection)

    def _apply_date_range(self) -> None:
        start_q = self.date_from.date()
        end_q = self.date_to.date()
        start = datetime(start_q.year(), start_q.month(), start_q.day(), 0, 0, 0)
        end = datetime(end_q.year(), end_q.month(), end_q.day(), 23, 59, 59)
        if start > end:
            start, end = end.replace(hour=0, minute=0, second=0), start.replace(hour=23, minute=59, second=59)
        selection = select_by_date_range(self._records, start, end)
        self._update_strip(selection)
        self._highlight_selection_on_map(selection)

    def _on_histogram_days_changed(self, days: object) -> None:
        self._histogram_selected_days = set(days) if isinstance(days, set) else set()
        if self.histogram_mode_radio.isChecked():
            self._apply_histogram_days()

    def _apply_histogram_days(self) -> None:
        selection = select_by_days(self._records, self._histogram_selected_days)
        self._update_strip(selection)
        self._highlight_selection_on_map(selection)
        self._update_histogram_label()

    def _update_histogram_label(self) -> None:
        selected = sorted(self._histogram_selected_days)
        if not selected:
            self.histogram_label.setText(tr("Keine Tage ausgewählt."))
            return
        if selected[0] == selected[-1]:
            label = selected[0].isoformat()
        else:
            label = f"{selected[0].isoformat()} – {selected[-1].isoformat()}"
        self.histogram_label.setText(tr("{count} Tage ausgewählt: {label}").format(count=len(selected), label=label))

    def _update_strip(self, selection: TripSelection) -> None:
        self._selection = selection
        self._thumb_token += 1
        token = self._thumb_token
        # Queued jobs from the previous selection would be decoded and then
        # dropped; mark them stale so the workers skip them entirely.
        self._thumbs.invalidate_before(token)
        self.strip.clear()
        self._items_by_path.clear()

        for record in selection.included:
            item = QListWidgetItem()
            tags: list[str] = []
            tips: list[str] = []
            if record.path in selection.edge_day:
                tags.append("⚑")
                tips.append(tr("Erster/letzter Tag – Reisetag prüfen"))
            if record.has_gps:
                tags.append("📍")
                tips.append(tr("GPS-Treffer innerhalb der Fläche"))
            else:
                tags.append("🕘")
                tips.append(tr("Über den Zeitraum ergänzt (kein GPS)"))
            prefix = "".join(tags)
            time_str = record.local_dt.strftime("%Y-%m-%d %H:%M") if record.local_dt else tr("ohne Zeit")
            item.setText(f"{prefix} {record.path.name}")
            item.setToolTip(f"{record.path.name}\n{time_str}\n" + "\n".join(tips))
            item.setData(Qt.ItemDataRole.UserRole, str(record.path))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setSizeHint(QSize(self._thumb_display + 20, self._thumb_display + 34))
            self.strip.addItem(item)
            self._items_by_path[str(record.path)] = item
            cached = self._thumbs.cached(record.path, record.kind)
            if cached is not None:
                item.setIcon(QIcon(QPixmap.fromImage(cached)))
            else:
                self._thumbs.submit(record.path, record.kind, token)

        without_gps = sum(1 for r in selection.included if not r.has_gps)
        with_gps = len(selection.included) - without_gps
        parts = [tr("{count} Medien").format(count=len(selection.included))]
        if with_gps:
            parts.append(tr("📍 {count} mit GPS").format(count=with_gps))
        if without_gps:
            parts.append(tr("🕘 {count} über Zeitraum").format(count=without_gps))
        if selection.unplaceable:
            parts.append(tr("{count} ungeklärt (ohne Zeit/GPS)").format(count=len(selection.unplaceable)))
        self.count_label.setText(" · ".join(parts) if selection.included else tr("Noch keine Auswahl."))

        if selection.window is not None:
            window_str = f"{selection.window[0].date().isoformat()} – {selection.window[1].date().isoformat()}"
            self.window_label.setText(tr("Zeitraum: {window}").format(window=window_str))
        else:
            window_str = "—"
            self.window_label.setText("")

        LOGGER.info(
            "Lasso selection: %s included = %s mit GPS (Fläche) + %s ohne GPS (Zeitraum); "
            "Zeitfenster=%s; ungeklärt=%s",
            len(selection.included), with_gps, without_gps, window_str, len(selection.unplaceable),
        )

        self._suggest_name(selection)

    def _on_thumb_ready(self, path_str: str, image: object, token: int) -> None:
        if token != self._thumb_token:
            return
        item = self._items_by_path.get(path_str)
        if item is None:
            return
        if isinstance(image, QImage) and not image.isNull():
            item.setIcon(QIcon(QPixmap.fromImage(image)))
        else:
            item.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon))

    # -- folder name suggestion --------------------------------------------- #

    def _suggest_name(self, selection: TripSelection) -> None:
        if self._name_user_edited or not selection.included:
            return
        fallback = default_folder_name(selection.window)
        if not self.name_edit.text().strip():
            self.name_edit.setText(fallback)
        centroid = centroid_of(selection.included)
        if centroid is None:
            return
        year = selection.window[0].year if selection.window else None
        # Debounced: during histogram drags the selection changes per mouse-move;
        # only the settled selection triggers a (cached, token-checked) lookup.
        self._pending_geocode = (centroid[0], centroid[1], year)
        self._geocode_timer.start()

    def _start_geocode_lookup(self) -> None:
        if self._pending_geocode is None:
            return
        lat, lon, year = self._pending_geocode
        token = self._thumb_token
        threading.Thread(
            target=self._geocode_worker, args=(lat, lon, year, token), daemon=True
        ).start()

    def _geocode_worker(self, lat: float, lon: float, year: int | None, token: int) -> None:
        place = reverse_geocode_place(lat, lon)
        if not place:
            return
        name = sanitize_folder_name(f"{place} {year}" if year else place)
        if name:
            self._name_relay.suggested.emit(name, token)

    def _on_name_suggested(self, name: str, token: int) -> None:
        # A slow HTTP response for an older selection must not overwrite the
        # suggestion for the current one (or the user's own edit).
        if token != self._thumb_token or self._name_user_edited:
            return
        self.name_edit.setText(name)

    def _on_name_edited(self, _text: str) -> None:
        self._name_user_edited = True

    # -- destination / transfer --------------------------------------------- #

    def _choose_dest(self) -> None:
        start = self.dest_edit.text() or (str(self._default_root) if self._default_root else "")
        chosen = QFileDialog.getExistingDirectory(self, tr("Zielverzeichnis wählen"), start)
        if chosen:
            self.dest_edit.setText(chosen)

    def _dest_one_level_up(self) -> None:
        text = self.dest_edit.text().strip()
        if text:
            self.dest_edit.setText(str(parent_directory(Path(text))))

    def _checked_sources(self) -> list[Path]:
        sources: list[Path] = []
        for index in range(self.strip.count()):
            item = self.strip.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                sources.append(Path(item.data(Qt.ItemDataRole.UserRole)))
        return sources

    def _on_transfer(self) -> None:
        if self._transfer_thread is not None:
            return  # a transfer is already running
        sources = self._checked_sources()
        if not sources:
            QMessageBox.warning(self, tr("Reise-Lasso"), tr("Es sind keine Medien ausgewählt."))
            return
        base = self.dest_edit.text().strip()
        name = sanitize_folder_name(self.name_edit.text().strip())
        if not base or not name:
            QMessageBox.warning(self, tr("Reise-Lasso"), tr("Bitte Zielverzeichnis und Ordnername angeben."))
            return
        copy = self.copy_checkbox.isChecked()
        target_dir = Path(base) / name
        verb = tr("kopieren") if copy else tr("verschieben")
        confirm = QMessageBox.question(
            self,
            tr("Reise-Lasso"),
            tr("{count} Medien nach\n{target}\n{verb}?").format(count=len(sources), target=target_dir, verb=verb),
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        # Run on a worker thread: moving thousands of files (or cross-volume
        # copies) would otherwise freeze the dialog with no progress feedback.
        self.move_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.count_label.setText(tr("Übertrage {done} / {total} …").format(done=0, total=len(sources)))
        self._transfer_thread = QThread(self)
        self._transfer_worker = TransferWorker(sources, target_dir, copy)
        self._transfer_worker.moveToThread(self._transfer_thread)
        self._transfer_thread.started.connect(self._transfer_worker.run)
        self._transfer_worker.progressed.connect(
            lambda done, total: self.count_label.setText(tr("Übertrage {done} / {total} …").format(done=done, total=total))
        )
        self._transfer_worker.finished.connect(
            lambda moved, errors: self._on_transfer_finished(moved, errors, target_dir, copy)
        )
        self._transfer_worker.finished.connect(self._transfer_thread.quit)
        self._transfer_thread.finished.connect(self._transfer_worker.deleteLater)
        self._transfer_thread.start()

    def _on_transfer_finished(
        self,
        moved: list[Path],
        errors: list[tuple[Path, str]],
        target_dir: Path,
        copy: bool,
    ) -> None:
        thread = self._transfer_thread
        self._transfer_thread = None
        self._transfer_worker = None
        if thread is not None:
            thread.quit()
            thread.wait(2000)
        self.move_button.setEnabled(True)
        self.cancel_button.setEnabled(True)

        self.moved_sources = moved
        self.copied = copy
        self.target_dir = target_dir
        self.load_target_after_move_requested = (not copy) and self.load_target_checkbox.isChecked()

        if errors:
            preview = "\n".join(f"• {src.name}: {msg}" for src, msg in errors[:8])
            more = "\n" + tr("… und {count} weitere").format(count=len(errors) - 8) if len(errors) > 8 else ""
            QMessageBox.warning(
                self,
                tr("Reise-Lasso"),
                tr("{moved} übertragen, {failed} fehlgeschlagen:\n{details}").format(
                    moved=len(moved), failed=len(errors), details=preview + more
                ),
            )
        if moved:
            if self.load_target_after_move_requested:
                self.accept()
            elif copy:
                QMessageBox.information(self, tr("Reise-Lasso"), tr("{count} Medien kopiert.").format(count=len(moved)))
                self.count_label.setText(tr("{count} Medien kopiert.").format(count=len(moved)))
            else:
                self._records = remaining_records(self._records, moved)
                self._last_polygon = None
                self._histogram_selected_days = set()
                self.day_histogram.set_records(self._records)
                self.day_histogram.set_selected_dates(set())
                self._update_histogram_label()
                self._name_user_edited = False
                self.name_edit.clear()
                self._update_strip(TripSelection())
                moved_ids = json.dumps([str(path) for path in moved])
                self._run_javascript(
                    "if (typeof drawn !== 'undefined') { drawn.clearLayers(); }"
                    f" removeMarkers({moved_ids}); highlight([]);"
                )
                self.activePathsRemoved.emit(moved)
                self.count_label.setText(tr("Auswahl verschoben. Bereit für den nächsten Umkreis."))
        elif errors:
            self.count_label.setText(tr("{count} Übertragung(en) fehlgeschlagen.").format(count=len(errors)))
        else:
            self.count_label.setText(tr("Nichts übertragen."))

    # -- lifecycle --------------------------------------------------------- #

    def _transfer_running(self) -> bool:
        return self._transfer_thread is not None and self._transfer_thread.isRunning()

    def reject(self) -> None:  # noqa: D102 - Qt override (Esc / Abbrechen)
        if self._transfer_running():
            return
        super().reject()

    def _cleanup(self, *_args) -> None:
        """Stop background work and remove the temp map file (idempotent)."""
        self._thumbs.stop()
        thread = self._transfer_thread
        if thread is not None and thread.isRunning():
            # Safety net: a live QThread must never be destroyed with the dialog.
            thread.quit()
            while not thread.wait(2000):
                LOGGER.warning("Waiting for running transfer to finish before closing lasso dialog")
        path = getattr(self, "_map_html_path", None)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            self._map_html_path = None

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._transfer_running():
            QMessageBox.information(
                self, tr("Reise-Lasso"), tr("Bitte warten, bis die laufende Übertragung abgeschlossen ist.")
            )
            event.ignore()
            return
        self._cleanup()
        super().closeEvent(event)
