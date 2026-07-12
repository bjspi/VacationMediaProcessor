"""Window frame geometry persistence for the main window (mixin)."""

from __future__ import annotations

from PyQt6.QtCore import QByteArray, QPoint, QRect
from PyQt6.QtWidgets import QApplication

from ...core.logging_config import get_logger

LOGGER = get_logger(__name__)


class WindowGeometryMixin:
    """Save/restore of the main window frame geometry and maximized state."""

    def _restore_window_geometry(self) -> bool:
        """Restore the saved window frame geometry and state; True on success.

        Uses Qt's ``restoreGeometry`` so the window frame, position and the
        maximized/full-screen state round-trip correctly (plain x/y/size
        fields would lose the title-bar height and the maximized state).
        """
        blob = self.settings_model.main_window_geometry
        if not blob:
            return False
        try:
            return bool(self.restoreGeometry(QByteArray.fromBase64(blob.encode("ascii"))))
        except Exception:  # noqa: BLE001
            LOGGER.debug("Could not restore main window geometry", exc_info=True)
            return False

    def _initial_window_geometry(self) -> tuple[int, int, QPoint | None]:
        """Return persisted window geometry clamped to visible screen space."""
        width = max(640, int(self.settings_model.window_width))
        height = max(480, int(self.settings_model.window_height))
        available = self._available_screen_geometry()
        if available is None:
            return width, height, None
        max_width = max(640, available.width() - 80)
        max_height = max(480, available.height() - 120)
        width = min(width, max_width)
        height = min(height, max_height)
        if self.settings_model.window_x is None or self.settings_model.window_y is None:
            return width, height, None
        max_x = max(available.left(), available.right() - width + 1)
        max_y = max(available.top(), available.bottom() - height + 1)
        x = min(max(int(self.settings_model.window_x), available.left()), max_x)
        y = min(max(int(self.settings_model.window_y), available.top()), max_y)
        return width, height, QPoint(x, y)

    def _available_screen_geometry(self) -> QRect | None:
        """Return the available geometry covering the saved point or primary screen."""
        saved_x = self.settings_model.window_x
        saved_y = self.settings_model.window_y
        if saved_x is not None and saved_y is not None:
            screen = QApplication.screenAt(QPoint(int(saved_x), int(saved_y)))
            if screen is not None:
                return screen.availableGeometry()
        screen = QApplication.primaryScreen()
        if screen is None:
            return None
        return screen.availableGeometry()

    def _remember_window_geometry(self) -> None:
        """Store the current window geometry (frame, position, maximized) in settings."""
        self.settings_model.main_window_geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
        # Keep the plain fields in sync as a fallback for unrestorable blobs.
        size = self.size()
        position = self.pos()
        self.settings_model.window_x = position.x()
        self.settings_model.window_y = position.y()
        self.settings_model.window_width = max(640, size.width())
        self.settings_model.window_height = max(480, size.height())

