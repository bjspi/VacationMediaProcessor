"""Reusable custom widgets and table column configuration for the main window."""

from __future__ import annotations

from PyQt6.QtCore import QObject, QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPixmap, QPolygon
from PyQt6.QtWidgets import QHeaderView, QLabel, QPushButton, QSizePolicy, QTableWidget, QWidget

from ...core.i18n import tr


class MediaTableColumns:
    """Named table columns for the main media table."""

    ORDER: tuple[str, ...] = (
        "status",
        "type",
        "file",
        "date",
        "gps",
        "source",
        "conf",
        "action",
        "target",
        "bucket",
        "codec",
        "size",
    )
    LABELS: dict[str, str] = {
        "status": "Status",
        "type": "Typ",
        "file": "Datei",
        "date": "Datum",
        "gps": "GPS",
        "source": "Quelle",
        "conf": "Conf",
        "action": "Aktion",
        "target": "Ziel",
        "bucket": "Bucket",
        "codec": "Codec",
        "size": "Größe",
    }
    INDEX: dict[str, int] = {name: index for index, name in enumerate(ORDER)}

    @classmethod
    def index(cls, name: str) -> int:
        """Return the table index for a named column."""
        return cls.INDEX[name]

    @classmethod
    def labels(cls) -> list[str]:
        """Return table labels in display order (translated at call time)."""
        return [tr(cls.LABELS[name]) for name in cls.ORDER]


class MediaTableWidget(QTableWidget):
    """Table widget that turns Delete into an active-list removal request."""

    deletePressed = pyqtSignal()

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() == Qt.Key.Key_Delete:
            self.deletePressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


# Per-column fitting spec: name -> (min width px, shrink priority).
# Higher shrink priority columns give up width first when the table is too
# narrow to show every column at its content width; the small informative
# columns (priority 0) keep their width longest.
COLUMN_FIT: dict[str, tuple[int, int]] = {
    "status": (54, 0),
    "type": (46, 0),
    "file": (58, 3),
    "date": (96, 1),
    "gps": (38, 0),
    "source": (46, 3),
    "conf": (50, 0),
    "action": (72, 5),
    "target": (50, 4),
    "bucket": (48, 2),
    "codec": (78, 0),
    "size": (94, 0),
}
# Upper bounds so a single long cell can't make one column hog the width.
COLUMN_CAP: dict[str, int] = {
    "file": 320,
    "source": 200,
    "target": 300,
    "action": 800,
    "date": 200,
    "bucket": 160,
}


def distribute_column_widths(
    desired: dict[str, int],
    mins: dict[str, int],
    priorities: dict[str, int],
    available: int,
    absorber: str,
) -> dict[str, int]:
    """Fit column widths into ``available``, shrinking by priority.

    When the desired widths fit, the surplus goes to ``absorber``. When they do
    not, the highest-priority (most shrinkable) columns give up width first, down
    to their minimum. If even the minimums exceed ``available`` the result sums to
    more than ``available`` (caller may then show a scrollbar).
    """
    result = dict(desired)
    total = sum(result.values())
    if total <= available:
        if absorber in result:
            result[absorber] += available - total
        return result
    deficit = total - available
    order = sorted(result, key=lambda n: (priorities.get(n, 0), result[n] - mins.get(n, 0)), reverse=True)
    for name in order:
        if deficit <= 0:
            break
        slack = result[name] - mins.get(name, 0)
        if slack <= 0:
            continue
        take = min(slack, deficit)
        result[name] -= take
        deficit -= take
    return result


class AspectRatioPreview(QLabel):
    """Preview label that scales its image to fit the available box.

    The full-resolution source pixmap is kept and rescaled (keeping aspect
    ratio) to the label's current size on every resize, so the preview grows
    when the settings section above is collapsed. The label ignores the pixmap
    for its own size hint (layout-driven via stretch), which avoids the
    width/height feedback loop that ``heightForWidth`` causes inside a
    scroll area.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setObjectName("preview")
        self.setMinimumHeight(160)
        self._source = QPixmap()
        self._placeholder = "Preview"
        self._rescaling = False
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

    def set_placeholder(self, text: str) -> None:
        """Set the text shown while no image is available."""
        self._placeholder = text
        if self._source.isNull():
            self.setText(text)

    def set_source_pixmap(self, pixmap: QPixmap | None) -> None:
        """Set (or clear) the preview image and rescale to fit the current box."""
        self._source = pixmap if pixmap is not None else QPixmap()
        if self._source.isNull():
            super().setPixmap(QPixmap())
            self.setText(self._placeholder)
        else:
            self.setText("")
            self._rescale()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if not self._source.isNull():
            self._rescale()

    def _rescale(self) -> None:
        if self._rescaling or self._source.isNull() or self.width() <= 0 or self.height() <= 0:
            return
        self._rescaling = True
        try:
            super().setPixmap(
                self._source.scaled(
                    self.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        finally:
            self._rescaling = False


class PreviewRelay(QObject):
    """Thread-safe relay that delivers a decoded preview image to the GUI thread."""

    ready = pyqtSignal(int, object)  # (token, QImage | None)


class LogRelay(QObject):
    """Thread-safe relay — carries a pyqtSignal for cross-thread logging."""

    message = pyqtSignal(str)


class FilterHeaderView(QHeaderView):
    """Horizontal header that shows a per-column filter indicator (Excel-style).

    Clicking a section emits ``filterClicked`` with the logical column index,
    which opens that column's filter popup.
    """

    filterClicked = pyqtSignal(int)
    _INDICATOR_WIDTH = 16

    def __init__(self, orientation: Qt.Orientation, parent: QWidget | None = None) -> None:
        super().__init__(orientation, parent)
        self.setSectionsClickable(True)
        self.setHighlightSections(True)
        self.setToolTip(tr("Spaltenkopf anklicken, um zu filtern (Text + Werteliste)."))
        self._active_columns: set[int] = set()
        # Clicking anywhere on a section opens that column's filter popup.
        self.sectionClicked.connect(self.filterClicked.emit)

    def set_active_columns(self, columns: set[int]) -> None:
        """Mark which columns currently have an active filter (for the glyph)."""
        self._active_columns = set(columns)
        self.viewport().update()

    def paintSection(self, painter, rect, logicalIndex) -> None:  # noqa: N802
        super().paintSection(painter, rect, logicalIndex)
        active = logicalIndex in self._active_columns
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#1d6fe0") if active else QColor("#6b7177"))
        cx = rect.right() - self._INDICATOR_WIDTH + 4
        cy = rect.center().y()
        triangle = QPolygon([QPoint(cx, cy - 2), QPoint(cx + 8, cy - 2), QPoint(cx + 4, cy + 3)])
        painter.drawPolygon(triangle)
        painter.restore()

    def sectionSizeFromContents(self, logicalIndex):  # noqa: N802
        """Reserve room on the right for the filter indicator."""
        size = super().sectionSizeFromContents(logicalIndex)
        size.setWidth(size.width() + self._INDICATOR_WIDTH)
        return size


class BadgeHeaderButton(QPushButton):
    """Compact header button with an overlaid numeric badge."""

    def __init__(self, badge_text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._badge = QLabel(badge_text, self)
        self._badge.setObjectName("badgeBubble")
        self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._badge.setFixedSize(18, 18)
        self._badge.raise_()

    def set_badge_text(self, text: str) -> None:
        """Update the badge text and reposition the overlay.

        Large counts are capped to ``99+`` and the bubble width is bounded so it
        never grows wider than the button and spills out to the right.
        """
        display = text
        try:
            if int(text) > 99:
                display = "99+"
        except (TypeError, ValueError):
            pass
        self._badge.setText(display)
        self._badge.adjustSize()
        width = max(18, min(self._badge.sizeHint().width() + 6, 28))
        self._badge.setFixedSize(width, 18)
        self._position_badge()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        """Keep the badge pinned to the top-right corner."""
        super().resizeEvent(event)
        self._position_badge()

    def _position_badge(self) -> None:
        """Place the badge in the top-right corner, growing leftward over the icon."""
        badge = self._badge
        # Right-align to the button's right edge so a wider bubble extends left
        # over the icon instead of spilling past the right edge.
        x = max(0, self.width() - badge.width())
        badge.move(x, 0)
        badge.raise_()
