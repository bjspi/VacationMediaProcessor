"""Day histogram widget with click/drag selection for trip extraction."""

from __future__ import annotations

from datetime import date

from PyQt6.QtCore import QRect, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from ...core.i18n import tr
from .trip_selection import TripRecord, build_day_buckets


class DayHistogramWidget(QWidget):
    """Day histogram with click/drag selection for trip extraction."""

    selectionChanged = pyqtSignal(object)  # set[date]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._buckets = []
        self._selected: set[date] = set()
        self._paint_state: bool | None = None
        self._last_anchor: date | None = None
        self.setMinimumHeight(150)
        self.setMouseTracking(True)

    def set_records(self, records: list[TripRecord]) -> None:
        """Rebuild buckets from the active lasso records."""
        self._buckets = build_day_buckets(records)
        known = {bucket.day for bucket in self._buckets}
        self._selected.intersection_update(known)
        if self._last_anchor not in known:
            self._last_anchor = None
        self.update()

    def set_selected_dates(self, days: set[date]) -> None:
        """Replace the selected days, dropping dates that are not visible."""
        known = {bucket.day for bucket in self._buckets}
        self._selected = set(days) & known
        self._last_anchor = max(self._selected) if self._selected else None
        self.update()

    def selected_dates(self) -> set[date]:
        """Return a copy of the currently selected days."""
        return set(self._selected)

    @staticmethod
    def day_label(day: date) -> str:
        """Return the compact day label shown above histogram bars."""
        return day.strftime("%d.%m.")

    def _bar_geometry(self) -> tuple[QRect, float, int]:
        """Return the (rect, bar_width, gap) used by both painting and hit-testing."""
        rect = self.rect().adjusted(8, 24, -8, -26)
        gap = 2
        bar_width = max(3, (rect.width() - gap * (len(self._buckets) - 1)) / len(self._buckets))
        return rect, bar_width, gap

    def date_at_position(self, x: int) -> date | None:
        """Map an x-position to the represented day bucket.

        Uses the identical geometry as :meth:`paintEvent`; mapping linearly over
        the full widget width would drift by up to a bar near the edges (the
        bars are inset by 8 px and clamped to a minimum width).
        """
        if not self._buckets or self.width() <= 0:
            return None
        rect, bar_width, gap = self._bar_geometry()
        index = int((x - rect.left()) / (bar_width + gap))
        index = max(0, min(len(self._buckets) - 1, index))
        return self._buckets[index].day

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#f8fafc"))
        if not self._buckets:
            painter.setPen(QColor("#64748b"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, tr("Keine datierten Medien"))
            return

        rect, bar_width, gap = self._bar_geometry()
        max_total = max(bucket.total for bucket in self._buckets)
        label_stride = max(1, int(34 / max(1, bar_width + gap)))
        painter.setPen(QPen(QColor("#cbd5e1"), 1))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())

        small_font = painter.font()
        small_font.setPointSize(max(7, small_font.pointSize() - 2))
        for index, bucket in enumerate(self._buckets):
            x = rect.left() + int(index * (bar_width + gap))
            height = max(4, int(rect.height() * bucket.total / max_total))
            y = rect.bottom() - height
            color = QColor("#2563eb") if bucket.day in self._selected else QColor("#94a3b8")
            painter.fillRect(x, y, int(bar_width), height, color)
            if bucket.gps_count:
                painter.fillRect(x, y, int(bar_width), 3, QColor("#16a34a"))
            if bucket.day in self._selected or index % label_stride == 0:
                painter.save()
                painter.setFont(small_font)
                painter.setPen(QColor("#334155"))
                label = self.day_label(bucket.day)
                label_width = painter.fontMetrics().horizontalAdvance(label)
                label_x = int(x + bar_width / 2 - label_width / 2)
                painter.drawText(label_x, max(10, y - 4), label)
                painter.restore()

        painter.setPen(QColor("#475569"))
        first = self._buckets[0].day.isoformat()
        last = self._buckets[-1].day.isoformat()
        painter.drawText(8, self.height() - 8, first)
        painter.drawText(self.width() - painter.fontMetrics().horizontalAdvance(last) - 8, self.height() - 8, last)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        day = self.date_at_position(int(event.position().x()))
        if day is None:
            return
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier and self._last_anchor is not None:
            self._select_range(self._last_anchor, day)
            self._paint_state = True
        else:
            self._paint_state = day not in self._selected
            self._set_day(day, self._paint_state)
            self._last_anchor = day
        self._emit_selection()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._paint_state is None or not (event.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(event)
            return
        day = self.date_at_position(int(event.position().x()))
        if day is None:
            return
        self._set_day(day, self._paint_state)
        self._last_anchor = day
        self._emit_selection()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._paint_state = None
        super().mouseReleaseEvent(event)

    def _set_day(self, day: date, selected: bool) -> None:
        if selected:
            self._selected.add(day)
        else:
            self._selected.discard(day)
        self.update()

    def _select_range(self, start: date, end: date) -> None:
        lo, hi = sorted((start, end))
        for bucket in self._buckets:
            if lo <= bucket.day <= hi:
                self._selected.add(bucket.day)
        self.update()

    def _emit_selection(self) -> None:
        self.selectionChanged.emit(set(self._selected))
