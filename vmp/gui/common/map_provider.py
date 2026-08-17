"""Shared Qt controls and WebEngine helpers for all map-based tools."""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtWidgets import QComboBox, QMessageBox, QWidget

from ...core.i18n import tr
from ...core.models import MapSettings
from ...map_providers import (
    MAP_PROVIDERS,
    leaflet_provider_switch_script,
    provider_configuration_error,
)


def configure_local_map_settings(settings) -> None:
    """Allow a file-backed map page to fetch CDN scripts and map tiles."""
    from PyQt6.QtWebEngineCore import QWebEngineSettings

    attributes = (
        QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,
        QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls,
        QWebEngineSettings.WebAttribute.JavascriptEnabled,
    )
    for attribute in attributes:
        settings.setAttribute(attribute, True)


def apply_map_provider(view, settings: MapSettings) -> None:
    """Switch the tile layer of an initialized Leaflet WebEngine view."""
    view.page().runJavaScript(leaflet_provider_switch_script(settings))


class MapProviderCombo(QComboBox):
    """Provider picker backed by the application's shared MapSettings object."""

    def __init__(
        self,
        settings: MapSettings,
        parent: QWidget | None = None,
        *,
        allow_unconfigured: bool = False,
        commit_on_change: bool = True,
        changed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._allow_unconfigured = allow_unconfigured
        self._commit_on_change = commit_on_change
        self._changed = changed
        if not allow_unconfigured and provider_configuration_error(settings.provider, settings.mapy_api_key):
            settings.provider = "osm"
        for provider in MAP_PROVIDERS:
            self.addItem(tr(provider.name), provider.id)
        index = self.findData(settings.provider)
        self.setCurrentIndex(max(index, 0))
        self.currentIndexChanged.connect(self._provider_changed)

    def selected_provider(self) -> str:
        """Return the provider id currently shown in the picker."""
        return str(self.currentData() or "osm")

    def set_changed_callback(self, changed: Callable[[], None] | None) -> None:
        """Set the callback invoked after a valid provider switch."""
        self._changed = changed

    def _provider_changed(self) -> None:
        provider_id = self.selected_provider()
        error = provider_configuration_error(provider_id, self._settings.mapy_api_key)
        if error and not self._allow_unconfigured:
            QMessageBox.warning(
                self,
                tr("Mapy.com API-Key fehlt"),
                tr("Bitte den Mapy.com API-Key zuerst unter Settings > Karten eintragen."),
            )
            self.blockSignals(True)
            previous = self.findData(self._settings.provider)
            self.setCurrentIndex(max(previous, 0))
            self.blockSignals(False)
            return
        if self._commit_on_change:
            self._settings.provider = provider_id
        if self._changed is not None:
            self._changed()
