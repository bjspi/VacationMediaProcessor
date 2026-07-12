"""Thumbnail strip widget with paint-style check toggling (Trip Lasso)."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QListWidget, QListWidgetItem, QStyledItemDelegate, QWidget

THUMB_DEFAULT_SIZE = 132  # default display size
THUMB_MIN = 90
THUMB_MAX = 240


class ThumbDelegate(QStyledItemDelegate):
    """Render deselected (unchecked) thumbnails with reduced opacity."""

    def paint(self, painter, option, index) -> None:  # noqa: N802 - Qt override
        state = index.data(Qt.ItemDataRole.CheckStateRole)
        checked = state == Qt.CheckState.Checked or (isinstance(state, int) and state == 2)
        if checked:
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setOpacity(0.4)
        super().paint(painter, option, index)
        painter.restore()



class ThumbStrip(QListWidget):
    """Icon strip with paint-style check toggling.

    A left click toggles the item under the cursor; holding the button and
    dragging across further items applies that same new check state to each one
    (drag to select or deselect a run of thumbnails).
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._paint_state: Qt.CheckState | None = None
        self._painted: set[int] = set()  # id() of items already painted this drag

    @staticmethod
    def _checkable(item: QListWidgetItem | None) -> bool:
        return item is not None and bool(item.flags() & Qt.ItemFlag.ItemIsUserCheckable)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.position().toPoint())
            if self._checkable(item):
                new = (
                    Qt.CheckState.Unchecked
                    if item.checkState() == Qt.CheckState.Checked
                    else Qt.CheckState.Checked
                )
                item.setCheckState(new)
                self._paint_state = new
                self._painted = {id(item)}
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._paint_state is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            item = self.itemAt(event.position().toPoint())
            if self._checkable(item) and id(item) not in self._painted:
                item.setCheckState(self._paint_state)
                self._painted.add(id(item))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._paint_state is not None and event.button() == Qt.MouseButton.LeftButton:
            self._paint_state = None
            self._painted = set()
            event.accept()
            return
        super().mouseReleaseEvent(event)


