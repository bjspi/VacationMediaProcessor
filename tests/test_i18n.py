"""Guard tests for the language catalog infrastructure."""

from __future__ import annotations

import json
import unittest

from vmp.core import i18n
from vmp.core.i18n import (
    available_languages,
    i18n_dir,
    resolve_language,
    tr,
)


class LanguageResolutionTests(unittest.TestCase):
    def test_available_languages_include_source_and_english(self) -> None:
        languages = available_languages()
        self.assertIn("de", languages)
        self.assertIn("en", languages)

    def test_explicit_preference_wins(self) -> None:
        self.assertEqual(resolve_language("de", "en_US"), "de")
        self.assertEqual(resolve_language("en", "de_DE"), "en")

    def test_auto_follows_system_locale(self) -> None:
        self.assertEqual(resolve_language("auto", "de_DE"), "de")
        self.assertEqual(resolve_language("auto", "de-AT"), "de")
        self.assertEqual(resolve_language("auto", "en_US"), "en")
        # Unsupported system language falls back to English, not German.
        self.assertEqual(resolve_language("auto", "fr_FR"), "en")


class CatalogTests(unittest.TestCase):
    def test_english_catalog_parses_and_is_populated(self) -> None:
        payload = json.loads((i18n_dir() / "en.json").read_text(encoding="utf-8"))
        self.assertIsInstance(payload, dict)
        self.assertGreater(len(payload), 200)
        for key, value in payload.items():
            self.assertTrue(key.strip(), "empty catalog key")
            self.assertTrue(str(value).strip(), f"empty translation for: {key!r}")

    def test_placeholders_survive_translation(self) -> None:
        """Every {placeholder} in a German key must appear in its translation."""
        import re

        payload = json.loads((i18n_dir() / "en.json").read_text(encoding="utf-8"))
        placeholder = re.compile(r"\{[a-zA-Z_][^}]*\}")
        for key, value in payload.items():
            for name in placeholder.findall(key):
                self.assertIn(name, value, f"placeholder {name} missing in translation of {key!r}")

    def test_tr_translates_in_english_and_is_identity_in_german(self) -> None:
        sample = "Scan ausstehend"
        try:
            i18n.init_language("en")
            self.assertNotEqual(tr(sample), sample)
            self.assertEqual(tr("nicht im Katalog vorhanden"), "nicht im Katalog vorhanden")
            i18n.init_language("de")
            self.assertEqual(tr(sample), sample)
        finally:
            i18n.init_language("de")


if __name__ == "__main__":
    unittest.main()
