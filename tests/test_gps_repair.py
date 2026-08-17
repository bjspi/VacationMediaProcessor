"""Tests for missing-GPS suggestion and standalone write behavior."""

from __future__ import annotations

import json
import tempfile
import unittest
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from vmp.core.models import Confidence, GpsRepairSettings, MediaKind, RawMetadata
from vmp.core.settings import _settings_from_payload
from vmp.gps_repair import (
    GpsAssignment,
    GpsRepairEntryResult,
    GpsRepairRecord,
    GpsRepairReport,
    GpsSuggestionMethod,
    GpsSuggestionStatus,
    build_gps_suggestions,
)
from vmp.metadata import gps_coordinates
from vmp.pipeline.gps_repair import apply_gps_assignments, gps_write_arguments


BASE = datetime(2026, 7, 1, 12, 0, 0)


def record(
    name: str,
    minutes: int,
    *,
    lat: float | None = None,
    lon: float | None = None,
    make: str = "Apple",
    model: str = "iPhone 17",
    confidence: Confidence = Confidence.HIGH,
) -> GpsRepairRecord:
    root = Path("C:/trip")
    return GpsRepairRecord(
        path=root / name,
        root=root,
        kind=MediaKind.IMAGE,
        local_dt=BASE + timedelta(minutes=minutes),
        confidence=confidence,
        date_only=False,
        lat=lat,
        lon=lon,
        make=make,
        model=model,
    )


class GpsSuggestionTests(unittest.TestCase):
    def test_same_device_bracket_is_safe_and_time_weighted(self) -> None:
        before = record("before.jpg", 0, lat=52.0, lon=13.0)
        target = record("target.jpg", 5)
        after = record("after.jpg", 20, lat=52.0, lon=13.02)

        suggestion = build_gps_suggestions([before, target, after], GpsRepairSettings())[0]

        self.assertEqual(suggestion.status, GpsSuggestionStatus.SAFE)
        self.assertEqual(suggestion.method, GpsSuggestionMethod.INTERPOLATED)
        self.assertAlmostEqual(suggestion.lon or 0, 13.005, places=4)

    def test_cross_device_pair_requires_review_without_cluster(self) -> None:
        target = record("camera.jpg", 5, make="Canon", model="R6")
        anchors = [
            record("a.jpg", 0, lat=52.0, lon=13.0),
            record("b.jpg", 10, lat=52.0, lon=13.005),
        ]
        suggestion = build_gps_suggestions([*anchors, target], GpsRepairSettings())[0]
        self.assertEqual(suggestion.status, GpsSuggestionStatus.REVIEW)

    def test_stationary_cluster_promotes_cross_device_but_keeps_interpolation(self) -> None:
        target = record("camera.jpg", 15, make="Canon", model="R6")
        anchors = [
            record("a.jpg", 0, lat=52.0, lon=13.000),
            record("b.jpg", 10, lat=52.0, lon=13.001),
            record("c.jpg", 20, lat=52.0, lon=13.002),
        ]
        suggestion = build_gps_suggestions([*anchors, target], GpsRepairSettings())[0]
        self.assertEqual(suggestion.status, GpsSuggestionStatus.SAFE)
        self.assertEqual(suggestion.method, GpsSuggestionMethod.CLUSTER_SUPPORTED)
        self.assertAlmostEqual(suggestion.lon or 0, 13.0015, places=5)

    def test_single_same_device_anchor_is_safe_only_inside_two_minutes(self) -> None:
        anchor = record("a.jpg", 0, lat=52.0, lon=13.0)
        safe = build_gps_suggestions([anchor, record("safe.jpg", 2)], GpsRepairSettings())[0]
        review = build_gps_suggestions([anchor, record("review.jpg", 3)], GpsRepairSettings())[0]
        self.assertEqual(safe.status, GpsSuggestionStatus.SAFE)
        self.assertEqual(review.status, GpsSuggestionStatus.REVIEW)

    def test_low_confidence_time_has_map_context_only(self) -> None:
        anchor = record("a.jpg", 0, lat=52.0, lon=13.0)
        target = record("low.jpg", 1, confidence=Confidence.LOW)
        suggestion = build_gps_suggestions([anchor, target], GpsRepairSettings())[0]
        self.assertEqual(suggestion.status, GpsSuggestionStatus.MANUAL)
        self.assertFalse(suggestion.has_position)

    def test_pair_beyond_review_cap_is_manual(self) -> None:
        before = record("a.jpg", 0, lat=52.0, lon=13.0)
        target = record("target.jpg", 150)
        after = record("b.jpg", 300, lat=52.0, lon=13.01)
        suggestion = build_gps_suggestions([before, target, after], GpsRepairSettings())[0]
        self.assertEqual(suggestion.status, GpsSuggestionStatus.MANUAL)


class GpsMetadataTests(unittest.TestCase):
    def test_iso6709_quicktime_coordinates_are_read(self) -> None:
        self.assertEqual(
            gps_coordinates({"Keys:GPSCoordinates": "+52.520008+013.404954/"}),
            (52.520008, 13.404954),
        )

    def test_writer_uses_exif_for_png_and_iso6709_for_mp4(self) -> None:
        png = gps_write_arguments(Path("photo.png"), -33.5, -70.6)
        video = gps_write_arguments(Path("clip.mp4"), 52.52, 13.405)
        self.assertIn("-EXIF:GPSLatitudeRef=S", png)
        self.assertIn("-EXIF:GPSLongitudeRef=W", png)
        self.assertEqual(video, ["-Keys:GPSCoordinates=+52.520000+013.405000/"])

    def test_settings_load_and_clamp_gps_thresholds(self) -> None:
        settings = _settings_from_payload(
            {
                "gps_repair": {
                    "single_safe_minutes": 7,
                    "pair_safe_distance_km": 12.5,
                    "cluster_min_anchors": 1,
                    "review_single_max_minutes": 2,
                }
            }
        )
        self.assertEqual(settings.gps_repair.single_safe_minutes, 7)
        self.assertEqual(settings.gps_repair.pair_safe_distance_km, 12.5)
        self.assertEqual(settings.gps_repair.cluster_min_anchors, 3)
        self.assertEqual(settings.gps_repair.review_single_max_minutes, 7)

    def test_apply_creates_backup_manifest_and_accepts_verified_readback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "photo.png"
            path.write_bytes(b"image")
            target = GpsRepairRecord(
                path=path,
                root=root,
                kind=MediaKind.IMAGE,
                local_dt=BASE,
                confidence=Confidence.HIGH,
                date_only=False,
            )
            assignment = GpsAssignment(target, 52.52, 13.405, GpsSuggestionMethod.MANUAL)
            before = RawMetadata(str(path), {})
            after = RawMetadata(
                str(path),
                {"GPS:GPSLatitude": "52.52", "GPS:GPSLongitude": "13.405"},
            )
            with patch("vmp.pipeline.gps_repair._read_one", side_effect=[before, after]), patch(
                "vmp.pipeline.gps_repair.run_process"
            ):
                report = apply_gps_assignments(
                    [assignment],
                    _settings_from_payload({}),
                    create_backups=True,
                )
            self.assertEqual(report.changed, 1)
            self.assertTrue(report.entries[0].backup_path and report.entries[0].backup_path.is_file())
            self.assertEqual(len(report.manifest_paths), 1)
            self.assertTrue(report.manifest_paths[0].is_file())

    def test_apply_skips_a_file_that_gained_gps_without_overwriting_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "photo.png"
            path.write_bytes(b"image")
            target = GpsRepairRecord(
                path, root, MediaKind.IMAGE, BASE, Confidence.HIGH, False
            )
            assignment = GpsAssignment(target, 1.0, 2.0, GpsSuggestionMethod.MANUAL)
            current = RawMetadata(
                str(path), {"GPS:GPSLatitude": "52", "GPS:GPSLongitude": "13"}
            )
            with patch("vmp.pipeline.gps_repair._read_one", return_value=current), patch(
                "vmp.pipeline.gps_repair.run_process"
            ) as process:
                report = apply_gps_assignments(
                    [assignment], _settings_from_payload({}), create_backups=False
                )
            self.assertEqual(report.changed, 0)
            self.assertEqual(report.failed, 0)
            self.assertTrue(report.entries[0].skipped)
            self.assertIs(report.entries[0].readback, current)
            process.assert_not_called()

    def test_one_file_failure_does_not_block_later_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [root / "bad.png", root / "good.png"]
            for path in paths:
                path.write_bytes(b"image")
            assignments = [
                GpsAssignment(
                    GpsRepairRecord(path, root, MediaKind.IMAGE, BASE, Confidence.HIGH, False),
                    52.52,
                    13.405,
                    GpsSuggestionMethod.MANUAL,
                )
                for path in paths
            ]
            before = RawMetadata(str(paths[1]), {})
            after = RawMetadata(
                str(paths[1]),
                {"GPS:GPSLatitude": "52.52", "GPS:GPSLongitude": "13.405"},
            )
            with patch(
                "vmp.pipeline.gps_repair._read_one",
                side_effect=[RuntimeError("broken"), before, after],
            ), patch("vmp.pipeline.gps_repair.run_process"):
                report = apply_gps_assignments(
                    assignments, _settings_from_payload({}), create_backups=False
                )
            self.assertEqual(report.changed, 1)
            self.assertEqual(report.failed, 1)
            self.assertEqual(len(report.entries), 2)


class GpsRepairDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PyQt6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    @patch("vmp.gui.gps.dialog.webengine_available", return_value=False)
    def test_offline_dialog_starts_unchecked_and_safe_button_selects_only_safe(self, _webengine) -> None:
        from PyQt6.QtCore import Qt

        from vmp.gui.gps.dialog import GpsRepairDialog

        anchors = [
            record("before.jpg", 0, lat=52.0, lon=13.0),
            record("after.jpg", 10, lat=52.0, lon=13.005),
        ]
        target = record("target.jpg", 5)
        dialog = GpsRepairDialog(
            None,
            [*anchors, target],
            GpsRepairSettings(),
            thumbnail_workers=0,
        )
        try:
            item = dialog.tree.topLevelItem(0)
            self.assertEqual(item.checkState(0), Qt.CheckState.Unchecked)
            self.assertFalse(dialog.pin_apply_button.isEnabled())
            dialog._select_safe()
            self.assertEqual(item.checkState(0), Qt.CheckState.Checked)
            self.assertIn("QTreeWidget::indicator:checked", dialog.tree.styleSheet())
            self.assertIn("check.svg", dialog.tree.styleSheet())
            self.assertTrue(dialog.apply_button.isEnabled())
        finally:
            dialog.close()

    @patch("vmp.gui.gps.dialog.webengine_available", return_value=False)
    def test_geometry_position_is_relative_to_parent_top_left(self, _webengine) -> None:
        from PyQt6.QtCore import QPoint
        from PyQt6.QtWidgets import QWidget

        from vmp.gui.gps.dialog import GpsRepairDialog

        parent = QWidget()
        parent.move(80, 70)
        target = record("target.jpg", 5)
        first = GpsRepairDialog(None, [target], GpsRepairSettings(), thumbnail_workers=0)
        try:
            first.setParent(parent, first.windowFlags())
            first.move(parent.frameGeometry().topLeft() + QPoint(45, 35))
            saved = first.result_geometry()
        finally:
            first.close()

        parent.move(280, 190)
        restored = GpsRepairDialog(
            parent,
            [target],
            GpsRepairSettings(),
            geometry=saved,
            thumbnail_workers=0,
        )
        try:
            offset = restored.frameGeometry().topLeft() - parent.frameGeometry().topLeft()
            self.assertEqual(offset, QPoint(45, 35))
        finally:
            restored.close()
            parent.close()

    @patch("vmp.gui.gps.dialog.webengine_available", return_value=False)
    def test_map_always_contains_device_specific_used_anchors(self, _webengine) -> None:
        from vmp.gui.gps.dialog import GpsRepairDialog

        records = [
            record("iphone-before.jpg", 0, lat=52.0, lon=13.0),
            record("canon-1.jpg", 3, lat=52.1, lon=13.1, make="Canon", model="R6"),
            record("canon-2.jpg", 4, lat=52.2, lon=13.2, make="Canon", model="R6"),
            record("target.jpg", 5),
            record("canon-3.jpg", 6, lat=52.3, lon=13.3, make="Canon", model="R6"),
            record("canon-4.jpg", 7, lat=52.4, lon=13.4, make="Canon", model="R6"),
            record("iphone-after.jpg", 10, lat=52.0, lon=13.005),
        ]
        dialog = GpsRepairDialog(None, records, GpsRepairSettings(), thumbnail_workers=0)
        try:
            payload = json.loads(dialog._bridge.get_payload())
            used = {point["label"].split(" · ", 1)[0] for point in payload["context"] if point["immediate"]}
            self.assertEqual(used, {"iphone-before.jpg", "iphone-after.jpg"})
        finally:
            dialog.close()

    @patch("vmp.gui.gps.dialog.webengine_available", return_value=False)
    def test_apply_report_survives_repairing_the_last_missing_file(self, _webengine) -> None:
        """Writing the final missing positions empties the tree without a crash.

        Regression: tree.clear() re-emits selection changes for paths that the
        rebuilt (now empty) suggestion dict no longer contains; the KeyError
        escaping the Qt slot aborted the whole process via qFatal.
        """
        from PyQt6.QtWidgets import QMessageBox

        from vmp.gui.gps.dialog import GpsRepairDialog

        records = [
            record("anchor.jpg", 0, lat=52.0, lon=13.0),
            record("t1.jpg", 5),
            record("t2.jpg", 10),
        ]
        dialog = GpsRepairDialog(None, records, GpsRepairSettings(), thumbnail_workers=0)
        try:
            entries = []
            for name in ("t1.jpg", "t2.jpg"):
                path = Path("C:/trip") / name
                dialog.tree.clearSelection()
                dialog.tree.setCurrentItem(dialog._items[path])
                dialog._items[path].setSelected(True)
                dialog._draft_pin = (52.001, 13.001)
                dialog._apply_pin_to_selection()
                assignment = dialog._manual[path]
                readback = RawMetadata(
                    str(path),
                    {"GPS:GPSLatitude": "52.001", "GPS:GPSLongitude": "13.001"},
                )
                entries.append(
                    GpsRepairEntryResult(assignment=assignment, success=True, readback=readback)
                )
            report = GpsRepairReport(run_id="run", entries=entries)

            with patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Ok):
                dialog.apply_report(report)

            self.assertEqual(dialog.tree.topLevelItemCount(), 0)
            self.assertEqual(dialog.remaining_missing, 0)
            self.assertFalse(dialog._checked)
            self.assertFalse(dialog._manual)
            self.assertFalse(dialog.pin_apply_button.isEnabled())
        finally:
            dialog.close()

    @patch("vmp.gui.gps.dialog.webengine_available", return_value=False)
    def test_manual_position_advances_past_the_edited_selection(self, _webengine) -> None:
        from vmp.gui.gps.dialog import GpsRepairDialog

        records = [
            record("before.jpg", 0, lat=52.0, lon=13.0),
            record("target-1.jpg", 5),
            record("target-2.jpg", 10),
            record("target-3.jpg", 15),
            record("after.jpg", 20, lat=52.01, lon=13.01),
        ]
        dialog = GpsRepairDialog(None, records, GpsRepairSettings(), thumbnail_workers=0)
        try:
            target_1 = Path("C:/trip/target-1.jpg")
            target_2 = Path("C:/trip/target-2.jpg")
            target_3 = Path("C:/trip/target-3.jpg")
            first = dialog._items[target_1]
            second = dialog._items[target_2]
            dialog.tree.clearSelection()
            dialog.tree.setCurrentItem(first)
            first.setSelected(True)
            second.setSelected(True)
            dialog._draft_pin = (52.005, 13.005)

            dialog._apply_pin_to_selection()

            self.assertIn(target_1, dialog._manual)
            self.assertIn(target_2, dialog._manual)
            self.assertEqual(dialog._active_path(), target_3)
            self.assertEqual(dialog.tree.selectedItems(), [dialog._items[target_3]])
        finally:
            dialog.close()


class GpsMapConfigurationTests(unittest.TestCase):
    def test_anchor_tooltip_expands_only_after_a_longer_hover(self) -> None:
        from vmp.gui.gps.map_view import MAP_HTML

        self.assertIn("ANCHOR_PREVIEW_DELAY_MS=700", MAP_HTML)
        self.assertIn("bindExpandableAnchorTooltip(marker", MAP_HTML)
        self.assertIn("gps-media-expanded .gps-media-card img", MAP_HTML)
        self.assertIn("marker.on('mouseout',resetPreview)", MAP_HTML)

    def test_file_backed_map_may_load_leaflet_and_tiles(self) -> None:
        from unittest.mock import Mock, call

        from PyQt6.QtWebEngineCore import QWebEngineSettings

        from vmp.gui.gps.map_view import configure_local_map_settings

        settings = Mock()
        configure_local_map_settings(settings)

        self.assertEqual(
            settings.setAttribute.call_args_list,
            [
                call(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True),
                call(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True),
                call(QWebEngineSettings.WebAttribute.JavascriptEnabled, True),
            ],
        )


class GpsRepairCompletionTests(unittest.TestCase):
    def test_result_dialog_is_deferred_past_worker_finished_signal(self) -> None:
        from unittest.mock import Mock

        from vmp.core.models import AppSettings
        from vmp.gui.main.overlay_flow import OverlayFlowMixin

        class Flow(OverlayFlowMixin):
            pass

        flow = Flow()
        flow.results = []
        flow.plans = []
        flow._backup_paths = {}
        flow.settings_model = AppSettings()
        flow.status_label = Mock()
        flow._set_busy = Mock()
        flow._apply_table_filters = Mock()
        flow._update_missing_gps_badge = Mock()
        flow._gps_repair_dialog = Mock()
        callbacks = []
        report = GpsRepairReport(run_id="run")

        with patch(
            "vmp.gui.main.overlay_flow.QTimer.singleShot",
            side_effect=lambda _delay, callback: callbacks.append(callback),
        ):
            flow._gps_repair_finished(report)

        flow._gps_repair_dialog.apply_report.assert_not_called()
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        flow._gps_repair_dialog.apply_report.assert_called_once_with(report)


if __name__ == "__main__":
    unittest.main()
