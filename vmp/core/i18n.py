"""Runtime UI translation backed by JSON catalog files.

The source language of every user-visible string in the code is **German**.
Translations live as plain JSON files in ``vmp/assets/i18n/``
(one file per language, e.g. ``en.json`` mapping the exact German source string
to its translation). The set of available languages is derived from the files
present there — dropping in ``fr.json`` makes French selectable without any
code change.

Call :func:`tr` on user-visible strings at *usage* time (never at import time
for module/class constants) and apply ``.format(...)`` afterwards::

    label.setText(tr("{count} Medien entfernt").format(count=n))

The language is resolved once at startup (:func:`init_language`) from the user
setting (``auto`` or a language code); ``auto`` follows the system locale. The
default is German so unit tests (which never call ``init_language``) always see
the source strings.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

LOGGER = logging.getLogger("vmp.core.i18n")

DEFAULT_LANGUAGE = "de"  # the source language of the strings in the code
_I18N_DIR = Path(__file__).resolve().parents[1] / "assets" / "i18n"

_current_language = DEFAULT_LANGUAGE
_catalog: dict[str, str] = {}


def i18n_dir() -> Path:
    """Return the directory holding the language catalog files."""
    return _I18N_DIR


def available_languages() -> tuple[str, ...]:
    """Return all selectable language codes (source language + catalog files)."""
    codes = {DEFAULT_LANGUAGE}
    try:
        codes.update(path.stem.lower() for path in _I18N_DIR.glob("*.json"))
    except OSError:
        LOGGER.warning("Could not list language catalogs in %s", _I18N_DIR, exc_info=True)
    return tuple(sorted(codes))


def _load_catalog(language: str) -> dict[str, str]:
    """Load one language catalog; missing/broken files degrade to the source text."""
    if language == DEFAULT_LANGUAGE:
        return {}
    path = _I18N_DIR / f"{language}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("Language catalog unusable, falling back to source strings: %s", path, exc_info=True)
        return {}
    if not isinstance(payload, dict):
        LOGGER.warning("Language catalog is not an object, ignoring: %s", path)
        return {}
    return {str(key): str(value) for key, value in payload.items()}


def resolve_language(preference: str, system_locale: str | None = None) -> str:
    """Return the effective language for a preference ('auto' or a code)."""
    languages = available_languages()
    if preference in languages:
        return preference
    primary = (system_locale or "").replace("-", "_").lower().split("_")[0]
    if primary in languages:
        return primary
    if "en" in languages:
        return "en"  # sensible fallback for any non-German system
    return DEFAULT_LANGUAGE


def init_language(preference: str, system_locale: str | None = None) -> str:
    """Set the active UI language once at startup; returns the resolved code."""
    global _current_language, _catalog
    _current_language = resolve_language(preference, system_locale)
    _catalog = _load_catalog(_current_language)
    LOGGER.info("UI language resolved: preference=%s system=%s -> %s", preference, system_locale, _current_language)
    return _current_language


def current_language() -> str:
    """Return the active language code."""
    return _current_language


def tr(text: str) -> str:
    """Translate a German source string into the active language.

    Unknown strings fall back to the German source text, so a missing catalog
    entry can never break the UI.
    """
    if not _catalog:
        return text
    return _catalog.get(text, text)
