"""Unit tests for the pure Trip Lasso selection logic."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from PyQt6.QtGui import QImage

from vmp.gui.lasso.dialog import (
    _parent_directory,
    _remaining_records,
    _DayHistogramWidget,
    _ThumbRelay,
    _ThumbStrip,
    _thumb_cache_key,
    _unique_target,
    LassoDialog,
    ThumbnailService,
    perform_transfer,
)
from vmp.gui.lasso.trip_selection import (
    TripRecord,
    build_day_buckets,
    centroid_of,
    default_folder_name,
    point_in_polygon,
    select_by_date_range,
    select_by_days,
    select_by_polygon,
)

# A simple square around Mallorca-ish coordinates (lat, lon).
SQUARE: list[tuple[float, float]] = [
    (39.0, 2.0),
    (40.0, 2.0),
    (40.0, 4.0),
    (39.0, 4.0),
]


def rec(name: str, lat=None, lon=None, dt=None, date_only=False, kind="image") -> TripRecord:
    """Build a TripRecord with a synthetic path."""
    local_dt = datetime.fromisoformat(dt) if isinstance(dt, str) else dt
    return TripRecord(Path(f"/m/{name}"), lat, lon, local_dt, date_only=date_only, kind=kind)


class PointInPolygonTests(unittest.TestCase):
    def test_inside(self) -> None:
        self.assertTrue(point_in_polygon(39.5, 3.0, SQUARE))

    def test_outside(self) -> None:
        self.assertFalse(point_in_polygon(50.0, 3.0, SQUARE))
        self.assertFalse(point_in_polygon(39.5, 10.0, SQUARE))

    def test_degenerate_polygon(self) -> None:
        self.assertFalse(point_in_polygon(39.5, 3.0, [(39.0, 2.0), (40.0, 2.0)]))


class SelectByPolygonTests(unittest.TestCase):
    def test_anchors_and_time_bridging(self) -> None:
        records = [
            rec("a_in", 39.5, 3.0, "2024-06-12T10:00:00"),   # anchor (in polygon)
            rec("b_in", 39.6, 3.1, "2024-06-15T18:00:00"),   # anchor (in polygon)
            rec("c_nogps", None, None, "2024-06-13T09:00:00"),  # bridged (in window)
            rec("d_nogps_out", None, None, "2024-07-01T09:00:00"),  # out of window
            rec("e_gps_out", 10.0, 10.0, "2024-06-13T12:00:00"),  # gps elsewhere -> excluded
        ]
        sel = select_by_polygon(records, SQUARE)
        paths = {p.path.name for p in sel.included}
        self.assertEqual(paths, {"a_in", "b_in", "c_nogps"})
        self.assertEqual({a.path.name for a in sel.anchors}, {"a_in", "b_in"})
        self.assertIsNotNone(sel.window)

    def test_window_padded_to_whole_days(self) -> None:
        records = [
            rec("anchor", 39.5, 3.0, "2024-06-12T22:00:00"),
            rec("early_same_day", None, None, "2024-06-12T06:00:00"),  # earlier same day, bridged
        ]
        sel = select_by_polygon(records, SQUARE)
        self.assertIn("early_same_day", {p.path.name for p in sel.included})
        assert sel.window is not None
        self.assertEqual(sel.window[0].hour, 0)
        self.assertEqual(sel.window[0].minute, 0)

    def test_edge_day_flagging(self) -> None:
        records = [
            rec("first", 39.5, 3.0, "2024-06-12T08:00:00"),
            rec("middle", 39.5, 3.0, "2024-06-14T08:00:00"),
            rec("last", 39.5, 3.0, "2024-06-16T08:00:00"),
        ]
        sel = select_by_polygon(records, SQUARE)
        edge_names = {p.name for p in sel.edge_day}
        self.assertEqual(edge_names, {"first", "last"})

    def test_date_only_anchor_is_included(self) -> None:
        records = [
            rec("anchor", 39.5, 3.0, "2024-06-12T10:00:00"),
            rec("dateonly", None, None, "2024-06-12T00:00:00", date_only=True),
        ]
        sel = select_by_polygon(records, SQUARE)
        self.assertIn("dateonly", {p.path.name for p in sel.included})

    def test_anchor_without_time_still_included(self) -> None:
        records = [rec("gps_no_time", 39.5, 3.0, None)]
        sel = select_by_polygon(records, SQUARE)
        self.assertEqual({p.path.name for p in sel.included}, {"gps_no_time"})
        self.assertIsNone(sel.window)  # no temporal anchor

    def test_unplaceable_bucket(self) -> None:
        records = [
            rec("anchor", 39.5, 3.0, "2024-06-12T10:00:00"),
            rec("ghost", None, None, None),  # no gps, no time
        ]
        sel = select_by_polygon(records, SQUARE)
        self.assertEqual({p.path.name for p in sel.unplaceable}, {"ghost"})
        self.assertNotIn("ghost", {p.path.name for p in sel.included})

    def test_all_outside(self) -> None:
        records = [rec("far", 10.0, 10.0, "2024-06-12T10:00:00")]
        sel = select_by_polygon(records, SQUARE)
        self.assertEqual(sel.included, [])
        self.assertEqual(sel.anchors, [])

    def test_empty_input(self) -> None:
        sel = select_by_polygon([], SQUARE)
        self.assertEqual(sel.included, [])


class SelectByDateRangeTests(unittest.TestCase):
    def test_basic_range(self) -> None:
        records = [
            rec("in1", None, None, "2024-06-12T10:00:00"),
            rec("in2", 39.5, 3.0, "2024-06-14T10:00:00"),
            rec("out", None, None, "2024-07-01T10:00:00"),
            rec("notime", None, None, None),
        ]
        start = datetime(2024, 6, 12, 0, 0, 0)
        end = datetime(2024, 6, 19, 23, 59, 59)
        sel = select_by_date_range(records, start, end)
        self.assertEqual({p.path.name for p in sel.included}, {"in1", "in2"})
        self.assertEqual({p.path.name for p in sel.unplaceable}, {"notime"})
        self.assertEqual({a.path.name for a in sel.anchors}, {"in2"})


class DayHistogramTests(unittest.TestCase):
    def test_build_day_buckets_counts_media_per_day(self) -> None:
        records = [
            rec("a.jpg", 39.5, 3.0, "2024-06-12T10:00:00"),
            rec("b.mov", None, None, "2024-06-12T11:00:00", kind="video"),
            rec("c.jpg", None, None, "2024-06-13T09:00:00"),
            rec("notime.jpg", None, None, None),
        ]

        buckets = build_day_buckets(records)

        self.assertEqual([bucket.day for bucket in buckets], [date(2024, 6, 12), date(2024, 6, 13)])
        self.assertEqual(buckets[0].total, 2)
        self.assertEqual(buckets[0].images, 1)
        self.assertEqual(buckets[0].videos, 1)
        self.assertEqual(buckets[0].gps_count, 1)
        self.assertEqual(buckets[1].total, 1)
        self.assertEqual(buckets[1].images, 1)
        self.assertEqual(buckets[1].videos, 0)
        self.assertEqual(buckets[1].gps_count, 0)

    def test_select_by_days_uses_explicit_days_not_full_min_max_range(self) -> None:
        records = [
            rec("first.jpg", None, None, "2024-06-12T10:00:00"),
            rec("middle.jpg", None, None, "2024-06-13T10:00:00"),
            rec("last.jpg", 39.5, 3.0, "2024-06-14T10:00:00"),
            rec("notime.jpg", None, None, None),
        ]

        selection = select_by_days(records, {date(2024, 6, 12), date(2024, 6, 14)})

        self.assertEqual([record.path.name for record in selection.included], ["first.jpg", "last.jpg"])
        self.assertEqual({record.path.name for record in selection.anchors}, {"last.jpg"})
        self.assertEqual({path.name for path in selection.edge_day}, {"first.jpg", "last.jpg"})
        self.assertEqual({record.path.name for record in selection.unplaceable}, {"notime.jpg"})
        self.assertIsNotNone(selection.window)
        assert selection.window is not None
        self.assertEqual(selection.window[0].date(), date(2024, 6, 12))
        self.assertEqual(selection.window[1].date(), date(2024, 6, 14))

    def test_select_by_days_with_empty_selection_returns_no_window(self) -> None:
        records = [
            rec("dated.jpg", None, None, "2024-06-12T10:00:00"),
            rec("notime.jpg", None, None, None),
        ]

        selection = select_by_days(records, set())

        self.assertEqual(selection.included, [])
        self.assertIsNone(selection.window)
        self.assertEqual({record.path.name for record in selection.unplaceable}, {"notime.jpg"})


class HelperTests(unittest.TestCase):
    def test_centroid(self) -> None:
        records = [rec("a", 39.0, 2.0), rec("b", 41.0, 4.0), rec("c", None, None)]
        self.assertEqual(centroid_of(records), (40.0, 3.0))

    def test_centroid_none(self) -> None:
        self.assertIsNone(centroid_of([rec("a", None, None, "2024-06-12T10:00:00")]))

    def test_default_folder_name_single_day(self) -> None:
        win = (datetime(2024, 6, 12, 0, 0), datetime(2024, 6, 12, 23, 59))
        self.assertEqual(default_folder_name(win), "2024-06-12")

    def test_default_folder_name_range(self) -> None:
        win = (datetime(2024, 6, 12, 0, 0), datetime(2024, 6, 19, 23, 59))
        self.assertEqual(default_folder_name(win), "2024-06-12_2024-06-19")

    def test_default_folder_name_none(self) -> None:
        self.assertEqual(default_folder_name(None), "Auswahl")

    def test_parent_directory_moves_one_level_up(self) -> None:
        path = Path("D:/Fotos/2026/HandyImport")
        self.assertEqual(_parent_directory(path), Path("D:/Fotos/2026"))

    def test_parent_directory_keeps_root_unchanged(self) -> None:
        root = Path("D:/")
        self.assertEqual(_parent_directory(root), root)

    def test_remaining_records_removes_moved_paths(self) -> None:
        records = [rec("a.jpg"), rec("b.jpg"), rec("c.jpg")]

        remaining = _remaining_records(records, [Path("/m/b.jpg")])

        self.assertEqual([item.path.name for item in remaining], ["a.jpg", "c.jpg"])

    def test_thumb_cache_key_changes_with_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "photo.jpg"
            path.write_text("data")

            key_small = _thumb_cache_key(path, "image", 128)
            key_large = _thumb_cache_key(path, "image", 256)

        self.assertNotEqual(key_small, key_large)

    def test_thumbnail_services_share_ram_cache(self) -> None:
        relay_a = _ThumbRelay()
        relay_b = _ThumbRelay()

        first = ThumbnailService(None, relay_a, workers=0, cache_mode="ram")
        second = ThumbnailService(None, relay_b, workers=0, cache_mode="ram")
        try:
            self.assertIs(first._memory_cache, second._memory_cache)
        finally:
            first.stop()
            second.stop()

    def test_thumbnail_service_can_peek_shared_ram_cache(self) -> None:
        relay_a = _ThumbRelay()
        relay_b = _ThumbRelay()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "photo.jpg"
            path.write_text("data")
            first = ThumbnailService(None, relay_a, workers=0, cache_mode="ram")
            second = ThumbnailService(None, relay_b, workers=0, cache_mode="ram")
            try:
                image = QImage(4, 4, QImage.Format.Format_RGB32)
                image.fill(0x00FF00)
                first._memory_cache[_thumb_cache_key(path, "image", first._size)] = image

                cached = second.cached(path, "image")

                self.assertIs(cached, image)
            finally:
                first.stop()
                second.stop()


class TransferTests(unittest.TestCase):
    def test_move_creates_dest_and_removes_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "photo.jpg"
            src.write_text("data")
            dest = root / "trip"
            moved, errors = perform_transfer([src], dest, copy=False)
            self.assertEqual(moved, [src])
            self.assertEqual(errors, [])
            self.assertFalse(src.exists())
            self.assertTrue((dest / "photo.jpg").exists())

    def test_copy_keeps_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "photo.jpg"
            src.write_text("data")
            dest = root / "trip"
            moved, _ = perform_transfer([src], dest, copy=True)
            self.assertEqual(moved, [src])
            self.assertTrue(src.exists())
            self.assertTrue((dest / "photo.jpg").exists())

    def test_collision_gets_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "trip"
            dest.mkdir()
            (dest / "photo.jpg").write_text("existing")
            src = root / "photo.jpg"
            src.write_text("new")
            self.assertEqual(_unique_target(dest, "photo.jpg"), dest / "photo (1).jpg")
            perform_transfer([src], dest, copy=False)
            self.assertTrue((dest / "photo (1).jpg").exists())
            self.assertEqual((dest / "photo.jpg").read_text(), "existing")

    def test_missing_source_reported_as_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ghost = root / "gone.jpg"
            moved, errors = perform_transfer([ghost], root / "trip", copy=False)
            self.assertEqual(moved, [])
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0][0], ghost)


class ThumbStripToggleTests(unittest.TestCase):
    """Click toggles a thumbnail; dragging paints the same state over a run."""

    @classmethod
    def setUpClass(cls) -> None:
        from PyQt6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _strip_with_items(self, count: int):
        from PyQt6.QtCore import QSize, Qt
        from PyQt6.QtWidgets import QListWidgetItem

        strip = _ThumbStrip()
        strip.setViewMode(_ThumbStrip.ViewMode.IconMode)
        strip.setIconSize(QSize(120, 120))
        strip.setSelectionMode(_ThumbStrip.SelectionMode.NoSelection)
        strip.resize(800, 240)
        for index in range(count):
            item = QListWidgetItem(f"img{index}.jpg")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setSizeHint(QSize(140, 154))
            strip.addItem(item)
        strip.show()
        self.app.processEvents()
        return strip

    @staticmethod
    def _center(strip, item):
        return strip.visualItemRect(item).center()

    def _press(self, strip, pos) -> None:
        from PyQt6.QtCore import QEvent, QPointF, Qt
        from PyQt6.QtGui import QMouseEvent

        strip.mousePressEvent(
            QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(pos), QPointF(pos),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        )

    def _move(self, strip, pos) -> None:
        from PyQt6.QtCore import QEvent, QPointF, Qt
        from PyQt6.QtGui import QMouseEvent

        strip.mouseMoveEvent(
            QMouseEvent(QEvent.Type.MouseMove, QPointF(pos), QPointF(pos),
                        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        )

    def _release(self, strip, pos) -> None:
        from PyQt6.QtCore import QEvent, QPointF, Qt
        from PyQt6.QtGui import QMouseEvent

        strip.mouseReleaseEvent(
            QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(pos), QPointF(pos),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        )

    def test_single_click_toggles_item(self) -> None:
        from PyQt6.QtCore import Qt

        strip = self._strip_with_items(1)
        item = strip.item(0)
        pos = self._center(strip, item)
        self._press(strip, pos)
        self._release(strip, pos)
        self.assertEqual(item.checkState(), Qt.CheckState.Unchecked)
        self._press(strip, pos)
        self._release(strip, pos)
        self.assertEqual(item.checkState(), Qt.CheckState.Checked)

    def test_drag_paints_same_state_over_run(self) -> None:
        from PyQt6.QtCore import Qt

        strip = self._strip_with_items(3)
        items = [strip.item(i) for i in range(3)]
        # Press on the first (Checked -> Unchecked), then drag across the rest.
        self._press(strip, self._center(strip, items[0]))
        self._move(strip, self._center(strip, items[1]))
        self._move(strip, self._center(strip, items[2]))
        self._release(strip, self._center(strip, items[2]))
        for item in items:
            self.assertEqual(item.checkState(), Qt.CheckState.Unchecked)


class DayHistogramWidgetTests(unittest.TestCase):
    """The histogram widget keeps a day selection independent of the thumbnail strip."""

    @classmethod
    def setUpClass(cls) -> None:
        from PyQt6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_set_selected_dates_round_trips_known_days_only(self) -> None:
        widget = _DayHistogramWidget()
        widget.set_records(
            [
                rec("a.jpg", None, None, "2024-06-12T10:00:00"),
                rec("b.jpg", None, None, "2024-06-13T10:00:00"),
            ]
        )

        widget.set_selected_dates({date(2024, 6, 13), date(2024, 6, 20)})

        self.assertEqual(widget.selected_dates(), {date(2024, 6, 13)})

    def test_date_at_position_maps_horizontal_bars(self) -> None:
        widget = _DayHistogramWidget()
        widget.resize(300, 140)
        widget.set_records(
            [
                rec("a.jpg", None, None, "2024-06-12T10:00:00"),
                rec("b.jpg", None, None, "2024-06-13T10:00:00"),
                rec("c.jpg", None, None, "2024-06-14T10:00:00"),
            ]
        )

        self.assertEqual(widget.date_at_position(8), date(2024, 6, 12))
        self.assertEqual(widget.date_at_position(150), date(2024, 6, 13))
        self.assertEqual(widget.date_at_position(292), date(2024, 6, 14))

    def test_day_label_uses_day_and_month_without_year(self) -> None:
        self.assertEqual(_DayHistogramWidget.day_label(date(2024, 6, 7)), "07.06.")


class LassoDialogConstructionTests(unittest.TestCase):
    """Constructor-level Trip Lasso wiring tests that avoid loading the web map."""

    @classmethod
    def setUpClass(cls) -> None:
        from PyQt6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_configured_thumbnail_worker_count_is_forwarded_to_service(self) -> None:
        from unittest.mock import patch

        with patch.object(LassoDialog, "_build_ui"), patch.object(LassoDialog, "_prefill_dates"), patch.object(
            LassoDialog, "_update_strip"
        ), patch("vmp.gui.lasso.dialog.ThumbnailService") as thumbnail_service:
            LassoDialog(None, [], None, None, thumbnail_workers=7)

        self.assertEqual(thumbnail_service.call_args.kwargs["workers"], 7)

    def test_configured_thumbnail_display_size_is_used(self) -> None:
        from unittest.mock import patch

        with patch.object(LassoDialog, "_build_ui"), patch.object(LassoDialog, "_prefill_dates"), patch.object(
            LassoDialog, "_update_strip"
        ):
            dialog = LassoDialog(None, [], None, None, thumbnail_display_size=188)

        self.assertEqual(dialog.thumbnail_display_size(), 188)


if __name__ == "__main__":
    unittest.main()
