"""Regression tests for shared GUI widgets."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from vmp.gui.common.widgets import BadgeHeaderButton


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


if __name__ == "__main__":
    unittest.main()
