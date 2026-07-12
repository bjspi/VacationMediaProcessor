"""Tests for IMG_/IMG_E pair detection, depth flagging, and skip-depth planning."""

from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path

from vmp.metadata import _detect_depth
from vmp.core.models import (
    AnalysisResult,
    ApplyMode,
    AppSettings,
    Confidence,
    MediaItem,
    MediaKind,
    PlanStatus,
    RawMetadata,
    ResolvedTimestamp,
)
from vmp.pair_cleanup import find_pairs
from vmp.planner import final_extension, keeps_depth_heic, needs_image_conversion

ROOT = Path("C:/pics")


def _result(name: str, width: int, height: int, dt: datetime, has_depth: bool = False) -> AnalysisResult:
    path = ROOT / name
    item = MediaItem(path, ROOT, MediaKind.IMAGE)
    resolved = ResolvedTimestamp(local_dt=dt, utc_dt=None, offset=None, confidence=Confidence.HIGH)
    return AnalysisResult(
        item=item,
        metadata=RawMetadata(str(path), {}),
        resolved=resolved,
        status=PlanStatus.OK,
        width=width,
        height=height,
        has_depth=has_depth,
    )


class FindPairsTests(unittest.TestCase):
    def test_crop_pair_detected_and_smaller_is_edit(self) -> None:
        dt = datetime(2026, 4, 3, 13, 42, 32)
        base = _result("IMG_1781.HEIC", 3024, 4032, dt)
        edit = _result("IMG_E1781.HEIC", 2268, 4032, dt)
        pairs = find_pairs([base, edit])
        self.assertEqual(len(pairs), 1)
        pair = pairs[0]
        self.assertEqual(pair.kind, "crop")
        self.assertTrue(pair.is_crop)
        self.assertEqual(pair.smaller_path.name, "IMG_E1781.HEIC")
        self.assertEqual(pair.bigger_path.name, "IMG_1781.HEIC")

    def test_same_dim_is_look_not_crop(self) -> None:
        dt = datetime(2026, 4, 5, 14, 52, 52)
        base = _result("IMG_1982.HEIC", 3024, 4032, dt)
        edit = _result("IMG_E1982.HEIC", 3024, 4032, dt)
        pairs = find_pairs([base, edit])
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].kind, "look")
        self.assertFalse(pairs[0].is_crop)
        # For a look pair the edit is still the one offered for deletion.
        self.assertEqual(pairs[0].smaller_path.name, "IMG_E1982.HEIC")

    def test_no_pair_without_base(self) -> None:
        dt = datetime(2026, 4, 3, 13, 42, 32)
        edit = _result("IMG_E1781.HEIC", 2268, 4032, dt)
        self.assertEqual(find_pairs([edit]), [])

    def test_different_capture_time_not_paired(self) -> None:
        base = _result("IMG_1781.HEIC", 3024, 4032, datetime(2026, 4, 3, 13, 42, 32))
        edit = _result("IMG_E1781.HEIC", 2268, 4032, datetime(2026, 4, 3, 15, 0, 0))
        self.assertEqual(find_pairs([base, edit]), [])

    def test_different_folders_not_paired(self) -> None:
        dt = datetime(2026, 4, 3, 13, 42, 32)
        base = _result("a/IMG_1781.HEIC", 3024, 4032, dt)
        edit = _result("b/IMG_E1781.HEIC", 2268, 4032, dt)
        self.assertEqual(find_pairs([base, edit]), [])

    def test_cross_suffix_collision_ignored(self) -> None:
        dt = datetime(2026, 4, 3, 13, 42, 32)
        base = _result("IMG_1781.MOV", 3024, 4032, dt)
        base.item = MediaItem(ROOT / "IMG_1781.MOV", ROOT, MediaKind.VIDEO)
        edit = _result("IMG_E1781.HEIC", 2268, 4032, dt)
        # base is a video -> not an image pair
        self.assertEqual(find_pairs([base, edit]), [])


class DetectDepthTests(unittest.TestCase):
    def test_apple_depth_tags(self) -> None:
        self.assertTrue(_detect_depth({"XMP-depthData:DepthDataVersion": 65541}))
        self.assertTrue(_detect_depth({"XMP-depthBlurEffect:RenderingParameters": "..."}))

    def test_samsung_depth_tag(self) -> None:
        self.assertTrue(_detect_depth({"Samsung:DepthMapData": "(Binary data)"}))

    def test_no_depth(self) -> None:
        self.assertFalse(_detect_depth({"EXIF:DateTimeOriginal": "2026:01:01 00:00:00"}))


class KeepDepthHeicPlanningTests(unittest.TestCase):
    def _settings(self, skip: bool) -> AppSettings:
        settings = AppSettings()
        settings.metadata.apply_mode = ApplyMode.FULL_NORMALIZE
        settings.images.heic_heif_to_jpg = True
        settings.images.skip_depth_heic_conversion = skip
        return settings

    def test_depth_heic_kept_when_skip_enabled(self) -> None:
        dt = datetime(2026, 4, 5, 14, 52, 52)
        result = _result("IMG_1982.HEIC", 3024, 4032, dt, has_depth=True)
        settings = self._settings(skip=True)
        self.assertTrue(keeps_depth_heic(result, settings))
        self.assertFalse(needs_image_conversion(result, settings))
        self.assertEqual(final_extension(result, settings), ".heic")

    def test_depth_heic_converted_when_skip_disabled(self) -> None:
        dt = datetime(2026, 4, 5, 14, 52, 52)
        result = _result("IMG_1982.HEIC", 3024, 4032, dt, has_depth=True)
        settings = self._settings(skip=False)
        self.assertFalse(keeps_depth_heic(result, settings))
        self.assertTrue(needs_image_conversion(result, settings))
        self.assertEqual(final_extension(result, settings), ".jpg")

    def test_non_depth_heic_still_converted(self) -> None:
        dt = datetime(2026, 4, 5, 14, 52, 52)
        result = _result("IMG_2000.HEIC", 3024, 4032, dt, has_depth=False)
        settings = self._settings(skip=True)
        self.assertFalse(keeps_depth_heic(result, settings))
        self.assertTrue(needs_image_conversion(result, settings))
        self.assertEqual(final_extension(result, settings), ".jpg")


class CollisionSubsecTests(unittest.TestCase):
    def test_subsec_suffix_used_on_collision(self) -> None:
        from datetime import datetime as _dt

        from vmp.planner import resolve_collision_target

        parent = Path("C:/pics")
        fmt = "%Y%m%d_%H%M%S"
        reserved: set[Path] = set()
        base_dt = _dt(2026, 5, 5, 15, 56, 3, 450000)  # .450s -> 450 ms
        # First file claims the plain name.
        first = resolve_collision_target(parent, base_dt, fmt, ".jpg", parent / "a.jpg", reserved,
                                         microsecond=base_dt.microsecond, use_subsec=True)
        self.assertEqual(first.name, "20260505_155603.jpg")
        # Second file at the same second with ms -> gets the .450ms suffix.
        second = resolve_collision_target(parent, base_dt, fmt, ".jpg", parent / "b.jpg", reserved,
                                          microsecond=base_dt.microsecond, use_subsec=True)
        self.assertEqual(second.name, "20260505_155603.450ms.jpg")

    def test_numeric_suffix_when_no_subsec(self) -> None:
        from datetime import datetime as _dt

        from vmp.planner import resolve_collision_target

        parent = Path("C:/pics")
        fmt = "%Y%m%d_%H%M%S"
        reserved: set[Path] = set()
        base_dt = _dt(2026, 5, 5, 15, 56, 3, 0)  # no sub-second
        resolve_collision_target(parent, base_dt, fmt, ".jpg", parent / "a.jpg", reserved,
                                 microsecond=0, use_subsec=True)
        second = resolve_collision_target(parent, base_dt, fmt, ".jpg", parent / "b.jpg", reserved,
                                          microsecond=0, use_subsec=True)
        self.assertEqual(second.name, "20260505_155603-2.jpg")

    def test_numeric_suffix_when_toggle_off(self) -> None:
        from datetime import datetime as _dt

        from vmp.planner import resolve_collision_target

        parent = Path("C:/pics")
        fmt = "%Y%m%d_%H%M%S"
        reserved: set[Path] = set()
        base_dt = _dt(2026, 5, 5, 15, 56, 3, 450000)
        resolve_collision_target(parent, base_dt, fmt, ".jpg", parent / "a.jpg", reserved,
                                 microsecond=base_dt.microsecond, use_subsec=False)
        second = resolve_collision_target(parent, base_dt, fmt, ".jpg", parent / "b.jpg", reserved,
                                          microsecond=base_dt.microsecond, use_subsec=False)
        self.assertEqual(second.name, "20260505_155603-2.jpg")


if __name__ == "__main__":
    unittest.main()
