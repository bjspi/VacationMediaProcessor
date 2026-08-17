"""Tests for the centralized map provider configuration."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PyQt6.QtWidgets import QApplication

from vmp.core.models import AppSettings, MapSettings
from vmp.core.settings import _settings_from_payload, load_settings, save_settings
from vmp.gui.common.map_provider import MapProviderCombo
from vmp.gui.settings_dialog import SettingsDialog
from vmp.map_providers import (
    leaflet_provider_config,
    leaflet_provider_script,
    normalize_provider_id,
    provider_requires_api_key,
)


class MapProviderConfigurationTests(unittest.TestCase):
    def test_provider_definitions_generate_expected_leaflet_configuration(self) -> None:
        osm = leaflet_provider_config(MapSettings(provider="osm"))
        topo = leaflet_provider_config(MapSettings(provider="opentopo"))
        mapy = leaflet_provider_config(MapSettings(provider="mapy", mapy_api_key="a b&c"))
        aerial = leaflet_provider_config(MapSettings(provider="mapy_aerial", mapy_api_key="a b&c"))

        self.assertEqual(osm["url"], "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png")
        self.assertEqual(osm["maxZoom"], 19)
        self.assertEqual(topo["url"], "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png")
        self.assertEqual(topo["maxZoom"], 17)
        self.assertEqual(
            mapy["url"],
            "https://api.mapy.com/v1/maptiles/basic/256/{z}/{x}/{y}?apikey=a%20b%26c",
        )
        self.assertEqual(mapy["maxZoom"], 20)
        self.assertEqual(
            aerial["url"],
            "https://api.mapy.com/v1/maptiles/aerial/256/{z}/{x}/{y}?apikey=a%20b%26c",
        )
        self.assertEqual(aerial["maxZoom"], 20)
        self.assertTrue(provider_requires_api_key("mapy_aerial"))

    def test_missing_mapy_key_uses_osm_as_safe_runtime_fallback(self) -> None:
        config = leaflet_provider_config(MapSettings(provider="mapy"))
        self.assertEqual(config["id"], "osm")

    def test_invalid_provider_id_normalizes_to_osm(self) -> None:
        self.assertEqual(normalize_provider_id("not-a-provider"), "osm")

    def test_shared_javascript_owns_tile_layer_switching(self) -> None:
        script = leaflet_provider_script(MapSettings(provider="opentopo"))
        self.assertIn("function setTileProvider(config)", script)
        self.assertIn("map.removeLayer(tileLayer)", script)
        self.assertIn("tile.opentopomap.org", script)


class MapSettingsPersistenceTests(unittest.TestCase):
    def test_old_settings_without_map_section_default_to_osm(self) -> None:
        loaded = _settings_from_payload({"recursive": False})
        self.assertEqual(loaded.maps.provider, "osm")
        self.assertEqual(loaded.maps.mapy_api_key, "")

    def test_invalid_provider_falls_back_to_osm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_file = root / "settings.json"
            settings_file.write_text(json.dumps({"maps": {"provider": "invalid"}}), encoding="utf-8")
            with patch("vmp.core.settings.settings_path", return_value=settings_file), patch(
                "vmp.core.settings.fallback_settings_path", return_value=root / "fallback.json"
            ):
                loaded = load_settings()
        self.assertEqual(loaded.maps.provider, "osm")

    def test_mapy_api_key_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_file = root / "settings.json"
            with patch("vmp.core.settings.settings_dir", return_value=root), patch(
                "vmp.core.settings.settings_path", return_value=settings_file
            ), patch("vmp.core.settings.fallback_settings_path", return_value=root / "fallback.json"):
                original = AppSettings(maps=MapSettings(provider="mapy", mapy_api_key="secret-key"))
                save_settings(original)
                loaded = load_settings()
        self.assertEqual(loaded.maps.provider, "mapy")
        self.assertEqual(loaded.maps.mapy_api_key, "secret-key")


class MapProviderWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def test_tool_combo_updates_shared_settings_and_calls_common_callback(self) -> None:
        settings = MapSettings()
        changed = Mock()
        combo = MapProviderCombo(settings, changed=changed)
        combo.setCurrentIndex(combo.findData("opentopo"))

        self.assertEqual(settings.provider, "opentopo")
        changed.assert_called_once_with()

    def test_provider_selected_in_one_tool_is_preselected_in_the_next(self) -> None:
        shared = MapSettings(mapy_api_key="configured-key")
        lasso_combo = MapProviderCombo(shared)
        lasso_combo.setCurrentIndex(lasso_combo.findData("mapy"))

        gps_combo = MapProviderCombo(shared)

        self.assertEqual(shared.provider, "mapy")
        self.assertEqual(gps_combo.currentData(), "mapy")

    def test_settings_dialog_rejects_mapy_without_key(self) -> None:
        settings = AppSettings()
        dialog = SettingsDialog(settings)
        dialog.map_provider_combo.setCurrentIndex(dialog.map_provider_combo.findData("mapy"))
        with patch("vmp.gui.settings_dialog.QMessageBox.warning") as warning, patch(
            "vmp.gui.settings_dialog.save_settings"
        ) as save_mock:
            dialog._on_save()

        warning.assert_called_once()
        save_mock.assert_not_called()
        self.assertEqual(settings.maps.provider, "osm")

    def test_settings_dialog_saves_mapy_key_and_provider(self) -> None:
        settings = AppSettings()
        dialog = SettingsDialog(settings)
        dialog.map_provider_combo.setCurrentIndex(dialog.map_provider_combo.findData("mapy"))
        dialog.mapy_api_key_edit.setText("configured-key")
        with patch("vmp.gui.settings_dialog.save_settings") as save_mock:
            dialog._on_save()

        self.assertEqual(settings.maps.provider, "mapy")
        self.assertEqual(settings.maps.mapy_api_key, "configured-key")
        save_mock.assert_called_once_with(settings)

    def test_settings_dialog_enables_same_key_for_mapy_aerial(self) -> None:
        dialog = SettingsDialog(AppSettings())
        dialog.map_provider_combo.setCurrentIndex(dialog.map_provider_combo.findData("mapy_aerial"))

        self.assertTrue(dialog.mapy_api_key_edit.isEnabled())


if __name__ == "__main__":
    unittest.main()
