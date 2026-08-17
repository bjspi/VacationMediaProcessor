"""Regression tests for shared GUI widgets."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QWidget

from vmp.gui.common.widgets import BadgeHeaderButton
from vmp.gui.main.header import build_header


class BadgeHeaderButtonTests(unittest.TestCase):
    """The warning bubble is useful only when it has a positive count."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_zero_badge_is_hidden_and_positive_badge_is_visible(self) -> None:
        button = BadgeHeaderButton("0")
        self.assertTrue(button._badge.isHidden())

        button.set_badge_text("3")
        self.assertFalse(button._badge.isHidden())

        button.set_badge_text("0")
        self.assertTrue(button._badge.isHidden())

    def test_large_positive_badge_remains_capped(self) -> None:
        button = BadgeHeaderButton("123")
        self.assertFalse(button._badge.isHidden())
        self.assertEqual(button._badge.text(), "99+")

    def test_missing_gps_button_is_immediately_right_of_pairs(self) -> None:
        class WindowStub(QWidget):
            def _asset_icon(self, _name: str) -> QIcon:
                return QIcon()

            def __getattr__(self, _name: str):
                return lambda *args, **kwargs: None

        window = WindowStub()
        header = build_header(window)  # type: ignore[arg-type]
        action_row = header.layout().itemAt(1).layout()
        widgets = [action_row.itemAt(index).widget() for index in range(action_row.count())]
        pairs_index = widgets.index(window.pairs_button)
        self.assertIs(widgets[pairs_index + 1], window.gps_repair_button)


if __name__ == "__main__":
    unittest.main()
