"""Tests for missing-GPS suggestion and standalone write behavior."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from vmp.core.models import (
    Confidence,
    GpsRepairSettings,
    MediaKind,
    PlanStatus,
    RawMetadata,
)
from vmp.core.processes import ProcessResult
from vmp.core.settings import _settings_from_payload
from vmp.gps_repair import (
    GpsAssignment,
    GpsRepairEntryResult,
    GpsRepairRecord,
    GpsRepairReport,
    GpsSuggestionMethod,
    GpsSuggestionStatus,
    build_gps_suggestions,
    normalize_position,
)
from vmp.metadata import gps_coordinates
from vmp.pipeline.gps_repair import apply_gps_assignments, gps_write_arguments

BASE = datetime(2026, 7, 1, 12, 0, 0)


UPDATED_STDOUT = "    1 image files updated\n"
REFUSED_STDOUT = "    0 image files updated\n    1 image files unchanged\n"


def exiftool_result(stdout: str = UPDATED_STDOUT) -> ProcessResult:
    """Build the ExifTool process result the writer inspects."""
    return ProcessResult(args=[], returncode=0, stdout=stdout, stderr="")


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

    def test_writer_uses_exif_for_heic_because_exiftool_refuses_keys(self) -> None:
        # ExifTool silently declines "-Keys:GPSCoordinates" on HEIC/HEIF
        # ("0 image files updated", exit 0), and photo viewers read EXIF GPS
        # from a HEIC anyway.
        for name in ("shot.heic", "shot.HEIF"):
            with self.subTest(name=name):
                args = gps_write_arguments(Path(name), 52.52, 13.405)
                self.assertEqual(
                    args,
                    [
                        "-EXIF:GPSLatitude=52.52000000",
                        "-EXIF:GPSLatitudeRef=N",
                        "-EXIF:GPSLongitude=13.40500000",
                        "-EXIF:GPSLongitudeRef=E",
                    ],
                )

    def test_refused_exiftool_write_fails_with_the_actual_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "photo.heic"
            path.write_bytes(b"image")
            target = GpsRepairRecord(path, root, MediaKind.IMAGE, BASE, Confidence.HIGH, False)
            assignment = GpsAssignment(target, 52.52, 13.405, GpsSuggestionMethod.MANUAL)
            empty = RawMetadata(str(path), {})
            with patch("vmp.pipeline.gps_repair._read_one", return_value=empty), patch(
                "vmp.pipeline.gps_repair.run_process",
                return_value=exiftool_result(REFUSED_STDOUT),
            ):
                report = apply_gps_assignments(
                    [assignment], _settings_from_payload({}), create_backups=False
                )
        self.assertEqual(report.changed, 0)
        self.assertEqual(report.failed, 1)
        self.assertIn("0 image files updated", report.entries[0].error)

    def test_unwrapped_map_longitude_is_folded_back_into_range(self) -> None:
        # Leaflet reports 200.0 for a click on the world copy east of the
        # dateline; -160.0 is the same meridian.
        self.assertEqual(normalize_position(48.0, 200.0), (48.0, -160.0))
        self.assertEqual(normalize_position(48.0, -200.0), (48.0, 160.0))
        # An in-range value must survive bit-for-bit, without modulo drift.
        self.assertEqual(normalize_position(52.52, 13.405), (52.52, 13.405))
        self.assertIsNone(normalize_position(91.0, 13.0))
        self.assertIsNone(normalize_position(float("nan"), 13.0))

    def test_out_of_range_assignment_is_rejected_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "photo.png"
            path.write_bytes(b"image")
            target = GpsRepairRecord(path, root, MediaKind.IMAGE, BASE, Confidence.HIGH, False)
            assignment = GpsAssignment(target, 48.0, 200.0, GpsSuggestionMethod.MANUAL)
            with patch("vmp.pipeline.gps_repair._read_one") as read_one, patch(
                "vmp.pipeline.gps_repair.run_process"
            ) as process:
                report = apply_gps_assignments(
                    [assignment], _settings_from_payload({}), create_backups=False
                )
        self.assertEqual(report.failed, 1)
        self.assertIn("Ungültige Zielkoordinate", report.entries[0].error)
        process.assert_not_called()
        read_one.assert_not_called()

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
                "vmp.pipeline.gps_repair.run_process", return_value=exiftool_result()
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
                "vmp.pipeline.gps_repair.run_process", return_value=exiftool_result()
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
            ), patch("vmp.pipeline.gps_repair.run_process", return_value=exiftool_result()):
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

    @patch("vmp.gui.gps.dialog.webengine_available", return_value=False)
    def test_pin_from_the_page_is_wrapped_and_range_checked(self, _webengine) -> None:
        from vmp.gui.gps.dialog import GpsRepairDialog

        records = [record("anchor.jpg", 0, lat=52.0, lon=13.0), record("target.jpg", 5)]
        dialog = GpsRepairDialog(None, records, GpsRepairSettings(), thumbnail_workers=0)
        try:
            dialog._pin_moved(48.0, 200.0)
            self.assertEqual(dialog._draft_pin, (48.0, -160.0))

            # A latitude cannot be wrapped; keep the previous draft instead.
            dialog._pin_moved(120.0, 13.0)
            self.assertEqual(dialog._draft_pin, (48.0, -160.0))
        finally:
            dialog.close()


class GpsMapConfigurationTests(unittest.TestCase):
    def test_anchor_tooltip_expands_only_after_a_longer_hover(self) -> None:
        from vmp.gui.gps.map_view import MAP_HTML

        self.assertIn("ANCHOR_PREVIEW_DELAY_MS=700", MAP_HTML)
        self.assertIn("bindExpandableAnchorTooltip(marker", MAP_HTML)
        self.assertIn("gps-media-expanded .gps-media-card img", MAP_HTML)
        self.assertIn("marker.on('mouseout',resetPreview)", MAP_HTML)

    def test_map_reports_only_wrapped_pin_coordinates(self) -> None:
        from vmp.gui.gps.map_view import MAP_HTML

        # Leaflet hands out unwrapped longitudes on repeated world copies, so
        # every path to the bridge must go through reportPin()/wrap().
        self.assertIn("var wrapped=latlng.wrap();", MAP_HTML)
        self.assertIn("reportPin(pin.getLatLng())", MAP_HTML)
        self.assertIn("reportPin(e.latlng)", MAP_HTML)
        self.assertEqual(MAP_HTML.count("bridge.pin_moved("), 1)

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

    def test_repairing_a_video_keeps_its_ffprobe_fields(self) -> None:
        from unittest.mock import Mock

        from vmp.core.models import (
            AnalysisResult,
            AppSettings,
            MediaItem,
            MediaPlan,
            ResolvedTimestamp,
        )
        from vmp.gui.main.overlay_flow import OverlayFlowMixin

        root = Path("C:/trip")
        item = MediaItem(path=root / "clip.mp4", root=root, kind=MediaKind.VIDEO)
        # ExifTool alone reports none of these for a typical MP4; the scan
        # filled them in from FFprobe afterwards.
        scanned = AnalysisResult(
            item=item,
            metadata=RawMetadata(str(item.path), {}),
            resolved=ResolvedTimestamp(BASE, None, None, Confidence.HIGH),
            status=PlanStatus.OK,
            width=3840,
            height=2160,
            codec="hevc",
            fps=59.94,
        )

        class Flow(OverlayFlowMixin):
            pass

        flow = Flow()
        flow.results = [scanned]
        flow.plans = [MediaPlan(analysis=scanned)]
        flow._backup_paths = {}
        flow.settings_model = AppSettings()
        flow.status_label = Mock()
        flow._set_busy = Mock()
        flow._apply_table_filters = Mock()
        flow._update_missing_gps_badge = Mock()
        flow._refresh_table_row = Mock()
        flow._gps_repair_dialog = None

        target = GpsRepairRecord(item.path, root, MediaKind.VIDEO, BASE, Confidence.HIGH, False)
        entry = GpsRepairEntryResult(
            assignment=GpsAssignment(target, 52.52, 13.405, GpsSuggestionMethod.MANUAL),
            success=True,
            readback=RawMetadata(
                str(item.path),
                {"GPS:GPSLatitude": "52.52", "GPS:GPSLongitude": "13.405"},
            ),
        )
        flow._gps_repair_finished(GpsRepairReport(run_id="run", entries=[entry]))

        repaired = flow.results[0]
        self.assertIsNot(repaired, scanned)
        self.assertEqual(
            (repaired.width, repaired.height, repaired.codec, repaired.fps),
            (3840, 2160, "hevc", 59.94),
        )
        self.assertIs(flow.plans[0].analysis, repaired)


if __name__ == "__main__":
    unittest.main()
