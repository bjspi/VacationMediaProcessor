"""Guard tests for bundled asset paths.

Paths computed relative to ``__file__`` break silently when a module moves to
a different folder depth — every icon path the GUI references must resolve to
an existing file, independent of where the helpers live.
"""

from __future__ import annotations

import unittest

from vmp.gui.common.theme import app_icon_path, asset_path, main_window_stylesheet

# Every asset name referenced anywhere in the GUI (header buttons, menus,
# checkbox indicators, filter popups).
REFERENCED_ASSETS = (
    "check.svg",
    "check_dark.svg",
    "gear.svg",
    "lasso.svg",
    "pairs.svg",
    "rotate.svg",
)


class AssetPathTests(unittest.TestCase):
    def test_app_icon_exists(self) -> None:
        self.assertTrue(app_icon_path().is_file(), f"app icon missing: {app_icon_path()}")

    def test_all_referenced_assets_exist(self) -> None:
        for name in REFERENCED_ASSETS:
            with self.subTest(asset=name):
                path = asset_path(name)
                self.assertTrue(path.is_file(), f"asset missing: {path}")

    def test_stylesheet_references_existing_check_icon(self) -> None:
        stylesheet = main_window_stylesheet()
        self.assertNotIn("__CHECK_ICON__", stylesheet)
        self.assertIn(asset_path("check.svg").as_posix(), stylesheet)


if __name__ == "__main__":
    unittest.main()
