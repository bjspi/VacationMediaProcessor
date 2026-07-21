"""Regression tests for the media processing core."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from concurrent.futures import Future
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication, QMessageBox

from vmp.core.discovery import discover_media
from vmp.tools import convert_image, copy_all_metadata, maintain_jpeg, write_metadata
from vmp.gui.common.plan_display import codec_cell_text
from vmp.gui.main.window import MainWindow, _missing_pipeline_tools, distribute_column_widths
from vmp.gui.settings_dialog import SettingsDialog
from vmp.manifest import write_before_after_manifests
from vmp.metadata import analyze_item, metadata_write_tags, resolve_timestamp
from vmp.core.models import (
    ActionKind,
    AppSettings,
    ApplyItemUpdate,
    ApplyMode,
    Confidence,
    MediaItem,
    MediaKind,
    AnalysisResult,
    MediaPlan,
    PlannedAction,
    PlanStatus,
    RawMetadata,
    ResolvedTimestamp,
)
from vmp.core.processes import command_template_error, expand_command_template, launch_command_template
from vmp.planner import build_plans
from vmp.planner import (
    crf_for_video,
    video_bucket,
    video_bucket_label,
    video_fps_limit,
)
from vmp.pipeline import _cleanup_empty_generated_dirs, apply_plans, maintain_jpegs, scan_and_plan
from vmp.pipeline.scan import _enrich_video_with_ffprobe, _safe_frame_rate
from vmp.reports import display_video_codec, plan_action_summary, preview_row, resolution_class
from vmp.core.settings import load_settings, save_settings


class TimestampResolutionTests(unittest.TestCase):
    """Timestamp resolver regression tests."""

    def test_iso_without_offset_is_usable_local_candidate(self) -> None:
        """ISO datetimes without offsets must not crash or be discarded."""
        root = Path.cwd()
        item = MediaItem(root / "clip.mp4", root, MediaKind.VIDEO)
        raw = RawMetadata(
            source_file=str(item.path),
            tags={
                "Keys:CreationDate": "2024-01-02T03:04:05",
                "QuickTime:CreateDate": "2024:01:02 02:04:05",
            },
        )
        resolved = resolve_timestamp(item, raw, tolerance_seconds=180)
        self.assertEqual(resolved.local_dt.year, 2024)
        self.assertEqual(resolved.local_dt.hour, 3)

    def test_date_only_filename_is_marked_date_only(self) -> None:
        """Date-only filename matches must not become fabricated capture times."""
        root = Path.cwd()
        item = MediaItem(root / "IMG-20240102-WA0001.jpg", root, MediaKind.IMAGE)
        raw = RawMetadata(source_file=str(item.path), tags={})
        resolved = resolve_timestamp(item, raw, tolerance_seconds=180)
        self.assertTrue(resolved.local_date_only)

    def test_container_end_of_recording_is_corrected_to_start_via_duration(self) -> None:
        """Samsung containers hold the END time; duration must shift UTC to the start."""
        root = Path.cwd()
        # Filename start 19:23:52 local; container CreateDate is start_UTC (+2h) + 52s.
        item = MediaItem(root / "20240102_192352.mp4", root, MediaKind.VIDEO)
        raw = RawMetadata(
            source_file=str(item.path),
            tags={
                "QuickTime:CreateDate": "2024:01:02 17:24:44",
                "QuickTime:Duration": "0:00:52",
            },
        )
        resolved = resolve_timestamp(item, raw, tolerance_seconds=180)
        self.assertEqual(resolved.local_dt.strftime("%Y-%m-%d %H:%M:%S"), "2024-01-02 19:23:52")
        self.assertEqual(resolved.utc_dt.strftime("%Y-%m-%d %H:%M:%S"), "2024-01-02 17:23:52")
        self.assertEqual(resolved.offset, timedelta(hours=2))
        self.assertEqual(resolved.confidence, Confidence.HIGH)
        self.assertTrue(any("end-of-recording" in note for note in resolved.notes))

    def test_container_start_time_is_not_shifted_by_duration(self) -> None:
        """When the container already holds the start, duration must not move UTC."""
        root = Path.cwd()
        item = MediaItem(root / "20240102_192352.mp4", root, MediaKind.VIDEO)
        raw = RawMetadata(
            source_file=str(item.path),
            tags={
                "QuickTime:CreateDate": "2024:01:02 17:23:52",  # already the start (local-2h)
                "QuickTime:Duration": "0:00:52",
            },
        )
        resolved = resolve_timestamp(item, raw, tolerance_seconds=180)
        self.assertEqual(resolved.utc_dt.strftime("%Y-%m-%d %H:%M:%S"), "2024-01-02 17:23:52")
        self.assertFalse(any("end-of-recording" in note for note in resolved.notes))
        # A real container timestamp that matches the filename start is corroboration.
        self.assertEqual(resolved.confidence, Confidence.HIGH)
        self.assertIn("Container", resolved.source)

    def test_oldest_datetime_is_ignored_as_only_source(self) -> None:
        """Custom OldestDateTime composites must not become capture-time evidence."""
        root = Path.cwd()
        item = MediaItem(root / "cryptic.jpg", root, MediaKind.IMAGE)
        raw = RawMetadata(
            source_file=str(item.path),
            tags={"Composite:OldestDateTime": "2024:01:02 03:04:05"},
        )

        resolved = resolve_timestamp(item, raw, tolerance_seconds=180)

        self.assertIsNone(resolved.local_dt)
        self.assertEqual(resolved.confidence, Confidence.ZERO)
        self.assertEqual(resolved.source, "unresolved")

    def test_icc_profile_datetime_is_ignored_as_only_source(self) -> None:
        """ICC profile timestamps are color-profile metadata, not capture time."""
        root = Path.cwd()
        item = MediaItem(root / "DZXNE2966.JPG", root, MediaKind.IMAGE)
        raw = RawMetadata(
            source_file=str(item.path),
            tags={"ICC-header:ProfileDateTime": "2022:01:01 00:00:00"},
        )

        resolved = resolve_timestamp(item, raw, tolerance_seconds=180)

        self.assertIsNone(resolved.local_dt)
        self.assertEqual(resolved.confidence, Confidence.ZERO)
        self.assertEqual(resolved.source, "unresolved")

    def test_oldest_datetime_does_not_override_filename_datetime(self) -> None:
        """Ignored OldestDateTime values must not override a full filename datetime."""
        root = Path.cwd()
        item = MediaItem(root / "IMG_20240102_030405.jpg", root, MediaKind.IMAGE)
        raw = RawMetadata(
            source_file=str(item.path),
            tags={"Composite:OldestDateTime": "1999:01:01 00:00:00"},
        )

        resolved = resolve_timestamp(item, raw, tolerance_seconds=180)

        self.assertEqual(resolved.local_dt.strftime("%Y-%m-%d %H:%M:%S"), "2024-01-02 03:04:05")
        self.assertEqual(resolved.confidence, Confidence.MEDIUM)
        self.assertEqual(resolved.source, "System:FileName")


class PlannerTests(unittest.TestCase):
    """Dry-run planner regression tests."""

    def _video_result(self, width: int | None, height: int | None) -> tuple[AppSettings, AnalysisResult]:
        root = Path.cwd()
        item = MediaItem(root / "clip.mp4", root, MediaKind.VIDEO)
        settings = AppSettings()
        result = analyze_item(
            item,
            RawMetadata(str(item.path), {"Keys:CreationDate": "2024-01-01T12:00:00+01:00"}),
            settings.metadata,
        )
        result.width = width
        result.height = height
        result.status = PlanStatus.OK
        return settings, result

    def _image_result(self, suffix: str) -> tuple[AppSettings, AnalysisResult]:
        root = Path.cwd()
        item = MediaItem(root / f"photo{suffix}", root, MediaKind.IMAGE)
        settings = AppSettings()
        result = analyze_item(
            item,
            RawMetadata(str(item.path), {"Keys:CreationDate": "2024-01-01T12:00:00+01:00"}),
            settings.metadata,
        )
        result.status = PlanStatus.OK
        return settings, result

    def test_non_full_modes_do_not_transcode_or_change_video_extension(self) -> None:
        """Cleanup and rename modes must not perform lossy video conversion."""
        root = Path.cwd()
        item = MediaItem(root / "clip.mov", root, MediaKind.VIDEO)
        raw = RawMetadata(str(item.path), {"Keys:CreationDate": "2024-01-02T03:04:05+01:00"})
        for mode in (ApplyMode.RENAME_ONLY, ApplyMode.SAMSUNG_CLEANUP, ApplyMode.GPS_CLEANUP):
            settings = AppSettings()
            settings.metadata.apply_mode = mode
            plan = build_plans([analyze_item(item, raw, settings.metadata)], settings)[0]
            self.assertIsNotNone(plan.final_path)
            self.assertEqual(plan.final_path.suffix, ".mov")
            self.assertNotIn("video_transcode", {action.kind.value for action in plan.actions})

    def test_default_buckets_cover_720p_1080p_qhd_and_4k_thresholds(self) -> None:
        """The default thresholds should split FHD, QHD, and 4K buckets predictably."""
        settings, result = self._video_result(1280, 720)
        self.assertEqual(video_bucket(result, settings), "FHD")
        settings, result = self._video_result(1920, 1080)
        self.assertEqual(video_bucket(result, settings), "FHD")
        settings, result = self._video_result(2560, 1440)
        self.assertEqual(video_bucket(result, settings), "QHD")
        settings, result = self._video_result(3840, 2160)
        self.assertEqual(video_bucket(result, settings), "4K")

    def test_resolution_class_thresholds(self) -> None:
        """The display resolution label should classify by the long edge."""
        self.assertEqual(resolution_class(3840, 2160), "4K")
        self.assertEqual(resolution_class(2560, 1440), "QHD")
        self.assertEqual(resolution_class(1920, 1080), "FHD")
        self.assertEqual(resolution_class(1280, 720), "HD")
        self.assertEqual(resolution_class(640, 480), "SD")
        self.assertEqual(resolution_class(None, None), "")
        # Portrait clips are classified by the long edge, too.
        self.assertEqual(resolution_class(1080, 1920), "FHD")

    def test_display_video_codec_normalizes_hevc_and_avc(self) -> None:
        """Codec ids from ExifTool/FFprobe should normalize to x264/x265."""
        for raw in ("hevc", "hvc1", "hev1", "H.265", "x265"):
            self.assertEqual(display_video_codec(raw), "x265")
        for raw in ("h264", "avc1", "AVC Coding", "x264"):
            self.assertEqual(display_video_codec(raw), "x264")
        self.assertEqual(display_video_codec("av1"), "AV1")
        self.assertEqual(display_video_codec(None), "")

    def test_codec_cell_includes_resolution_and_rounded_fps(self) -> None:
        """The codec cell should compactly show source resolution and whole FPS."""
        _settings, result = self._video_result(3840, 2160)
        result.codec = "hevc"
        result.fps = 60000 / 1001

        self.assertEqual(codec_cell_text(result), "x265 [4K · 60 fps]")

    def test_codec_cell_keeps_existing_format_when_fps_is_unknown(self) -> None:
        """Videos without a usable frame rate should retain the old display."""
        _settings, result = self._video_result(1920, 1080)
        result.codec = "h264"

        self.assertEqual(codec_cell_text(result), "x264 [FHD]")

    def test_ffprobe_frame_rate_parser_handles_ntsc_and_fallback(self) -> None:
        """Rational FFprobe values should parse, with r_frame_rate as fallback."""
        self.assertAlmostEqual(_safe_frame_rate("60000/1001"), 59.94005994)
        self.assertEqual(_safe_frame_rate("0/0", "30/1"), 30.0)
        self.assertIsNone(_safe_frame_rate("0/0", None))

    def test_ffprobe_enrichment_adds_frame_rate(self) -> None:
        """Video scan enrichment should retain FFprobe's average frame rate."""
        settings, result = self._video_result(3840, 2160)
        result.codec = "hevc"
        payload = {
            "streams": [
                {
                    "codec_type": "video",
                    "width": 3840,
                    "height": 2160,
                    "codec_name": "hevc",
                    "avg_frame_rate": "60000/1001",
                    "r_frame_rate": "60/1",
                }
            ]
        }

        with patch("vmp.pipeline.scan.probe_video", return_value=payload):
            _enrich_video_with_ffprobe(result, settings)

        self.assertAlmostEqual(result.fps, 59.94005994)

    def test_qhd_bucket_uses_qhd_crf(self) -> None:
        """QHD videos should use the dedicated QHD CRF setting."""
        settings, result = self._video_result(2560, 1440)
        self.assertEqual(crf_for_video(result, settings), settings.videos.qhd_crf)

    def test_full_hd_limit_uses_fhd_bucket_and_crf_for_oversized_video(self) -> None:
        """Downscaled videos should use FHD encode settings while showing the source bucket."""
        settings, result = self._video_result(3840, 2160)
        settings.videos.limit_to_fhd = True
        settings.videos.fhd_crf = 23
        settings.videos.uhd_crf = 31
        self.assertEqual(video_bucket(result, settings), "4K")
        self.assertEqual(video_bucket_label(result, settings), "4K -> FHD")
        self.assertEqual(crf_for_video(result, settings), 23)

    def test_video_action_description_mentions_bucket_and_crf(self) -> None:
        """Video plans should surface the selected bucket and CRF in their action text."""
        settings, result = self._video_result(2560, 1440)
        plan = build_plans([result], settings)[0]
        descriptions = " ".join(action.description for action in plan.actions)
        self.assertIn("Bucket QHD, CRF 26", descriptions)

    def test_video_action_description_mentions_audio_transcode_mode(self) -> None:
        """Video transcode actions should expose planned audio handling."""
        expected = {
            "aac": "Audio -> AAC 160k.",
            "ac3": "Audio -> AC3 160k.",
            "copy": "Audio-Kopie, falls MP4-kompatibel.",
        }
        for codec, text in expected.items():
            settings, result = self._video_result(2560, 1440)
            settings.videos.audio_codec = codec
            settings.videos.audio_bitrate = "160k"

            plan = build_plans([result], settings)[0]
            descriptions = " ".join(action.description for action in plan.actions)

            self.assertIn(text, descriptions)

    def test_video_limit_to_full_hd_adds_downscale_action(self) -> None:
        """Enabling the video limit should add a separate downscale action for oversized videos."""
        settings, result = self._video_result(2560, 1440)
        settings.videos.limit_to_fhd = True
        plan = build_plans([result], settings)[0]
        action_kinds = [action.kind.value for action in plan.actions]
        self.assertIn("video_downscale", action_kinds)
        self.assertIn("Full HD", " ".join(action.description for action in plan.actions))

    def test_video_limit_to_full_hd_description_uses_effective_fhd_crf(self) -> None:
        """The transcode text should show the effective FHD bucket/CRF for downscaled 4K."""
        settings, result = self._video_result(3840, 2160)
        settings.videos.limit_to_fhd = True
        settings.videos.fhd_crf = 23
        settings.videos.uhd_crf = 31
        plan = build_plans([result], settings)[0]
        descriptions = " ".join(action.description for action in plan.actions)
        self.assertIn("Bucket 4K -> FHD, CRF 23", descriptions)
        self.assertNotIn("CRF 31", descriptions)

    def test_video_fps_limit_applies_only_above_30_fps(self) -> None:
        """The setting should cap high-frame-rate video without increasing lower rates."""
        settings, result = self._video_result(3840, 2160)
        settings.videos.limit_to_30_fps = True
        result.fps = 60000 / 1001
        self.assertEqual(video_fps_limit(result, settings), 30)

        result.fps = 30000 / 1001
        self.assertIsNone(video_fps_limit(result, settings))
        result.fps = 25
        self.assertIsNone(video_fps_limit(result, settings))

    def test_video_plan_mentions_30_fps_limit_for_60_fps_source(self) -> None:
        """Dry-run actions should make the enabled frame-rate reduction visible."""
        settings, result = self._video_result(3840, 2160)
        settings.videos.limit_to_30_fps = True
        result.fps = 60

        plan = build_plans([result], settings)[0]
        descriptions = " ".join(action.description for action in plan.actions)

        self.assertIn("max. 30 fps", descriptions)

    def test_video_plan_omits_30_fps_limit_for_30_fps_source(self) -> None:
        """A 30-fps source should not be unnecessarily frame-converted."""
        settings, result = self._video_result(1920, 1080)
        settings.videos.limit_to_30_fps = True
        result.fps = 30

        plan = build_plans([result], settings)[0]
        descriptions = " ".join(action.description for action in plan.actions)

        self.assertNotIn("max. 30 fps", descriptions)

    def test_heic_conversion_description_mentions_heic_to_jpeg(self) -> None:
        """HEIC source conversion should be labeled clearly for users."""
        settings, result = self._image_result(".heic")
        settings.images.heic_heif_jpeg_quality = 91
        plan = build_plans([result], settings)[0]
        descriptions = " ".join(action.description for action in plan.actions)
        self.assertIn("HEIC -> JPEG, Qualität 91", descriptions)

    def test_jpeg_conversion_description_mentions_reencode(self) -> None:
        """JPEG source conversion should be labeled as a re-encode."""
        settings, result = self._image_result(".jpg")
        plan = build_plans([result], settings)[0]
        descriptions = " ".join(action.description for action in plan.actions)
        self.assertIn("JPEG-Re-Encode", descriptions)
        self.assertIn("Qualität 83", descriptions)

    def test_jpeg_metadata_only_summary_is_explicit(self) -> None:
        """JPEG rename-only plans should surface that they are metadata-only work."""
        settings, result = self._image_result(".jpg")
        settings.metadata.apply_mode = ApplyMode.RENAME_ONLY
        plan = build_plans([result], settings)[0]
        summary = plan_action_summary(plan)
        self.assertTrue(summary.startswith("Nur JPEG-Metadaten"))
        self.assertIn("Umbenennen zu", summary)

    def test_preview_row_includes_video_bucket_and_crf(self) -> None:
        """The dry-run preview row should expose the selected video bucket/CRF."""
        settings, result = self._video_result(2560, 1440)
        result.resolved = ResolvedTimestamp(
            local_dt=datetime(2024, 1, 1, 12, 0, 0),
            utc_dt=None,
            offset=None,
            confidence=Confidence.HIGH,
        )
        plan = build_plans([result], settings)[0]
        row = preview_row(plan, settings)
        self.assertEqual(row[7], "QHD / CRF 26")

    def test_preview_row_shows_source_to_effective_bucket_when_downscaling(self) -> None:
        """The dry-run preview row should expose the effective bucket after FHD limiting."""
        settings, result = self._video_result(3840, 2160)
        settings.videos.limit_to_fhd = True
        plan = build_plans([result], settings)[0]
        row = preview_row(plan, settings)
        self.assertEqual(row[7], "4K -> FHD / CRF 25")

    def test_full_normalize_adds_filename_exif_write_action_for_filename_only_image(self) -> None:
        """Full normalize should visibly write filename-derived dates into metadata."""
        root = Path.cwd()
        item = MediaItem(root / "IMG_20240102_030405.jpg", root, MediaKind.IMAGE)
        settings = AppSettings()
        result = analyze_item(item, RawMetadata(str(item.path), {}), settings.metadata)

        plan = build_plans([result], settings)[0]

        action_kinds = [action.kind for action in plan.actions]
        self.assertIn(ActionKind.WRITE_EXIF_FROM_FILENAME, action_kinds)
        self.assertLess(
            action_kinds.index(ActionKind.WRITE_EXIF_FROM_FILENAME),
            action_kinds.index(ActionKind.METADATA_NORMALIZE),
        )

    def test_rename_only_does_not_add_filename_exif_write_action(self) -> None:
        """Rename only must not write EXIF/container dates from filename."""
        root = Path.cwd()
        item = MediaItem(root / "IMG_20240102_030405.jpg", root, MediaKind.IMAGE)
        settings = AppSettings()
        settings.metadata.apply_mode = ApplyMode.RENAME_ONLY
        result = analyze_item(item, RawMetadata(str(item.path), {}), settings.metadata)

        plan = build_plans([result], settings)[0]

        self.assertNotIn(ActionKind.WRITE_EXIF_FROM_FILENAME, [action.kind for action in plan.actions])


class DiscoveryTests(unittest.TestCase):
    """Discovery regression tests."""

    def test_discovery_ignores_app_and_dot_directories(self) -> None:
        """Generated app folders and dot folders must not be reprocessed."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "_VacationMediaProcessor_Backup").mkdir()
            (root / ".cache").mkdir()
            (root / "real").mkdir()
            (root / "_VacationMediaProcessor_Backup" / "a.jpg").write_bytes(b"a")
            (root / ".cache" / "b.jpg").write_bytes(b"b")
            (root / "real" / "c.jpg").write_bytes(b"c")
            discovered = [str(item.relative_path) for item in discover_media(root)]
        self.assertEqual(discovered, [str(Path("real") / "c.jpg")])


class ScanPipelineTests(unittest.TestCase):
    """Metadata scan batching and ordering regression tests."""

    def test_scan_uses_configured_parallel_exiftool_batches_and_keeps_result_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            items = [
                MediaItem(root / f"img{index}.jpg", root, MediaKind.IMAGE)
                for index in range(5)
            ]
            settings = AppSettings()
            settings.exiftool_read_batch_size = 2
            settings.exiftool_parallel_batches = 3
            executor_max_workers: list[int] = []

            def fake_read_metadata_batch(chunk: list[MediaItem], _exiftool: str) -> dict[Path, RawMetadata]:
                return {
                    item.path.resolve(): RawMetadata(
                        source_file=str(item.path),
                        tags={"SourceFile": str(item.path), "EXIF:DateTimeOriginal": "2024:01:01 12:00:00"},
                    )
                    for item in reversed(chunk)
                }

            class FakeExecutor:
                def __init__(self, max_workers: int) -> None:
                    executor_max_workers.append(max_workers)

                def __enter__(self):
                    return self

                def __exit__(self, *_args) -> None:
                    return None

                def submit(self, fn, *args):
                    future: Future = Future()
                    future.set_result(fn(*args))
                    return future

            with patch("vmp.pipeline.scan._resolve_required_tool"), patch(
                "vmp.pipeline.scan.discover_media",
                return_value=items,
            ), patch(
                "vmp.pipeline.scan.read_metadata_batch",
                side_effect=fake_read_metadata_batch,
            ) as read_metadata_batch, patch(
                "vmp.pipeline.scan.ThreadPoolExecutor",
                FakeExecutor,
            ):
                results, _plans = scan_and_plan(root, settings)

        self.assertEqual(executor_max_workers, [3])
        self.assertEqual([len(call.args[0]) for call in read_metadata_batch.call_args_list], [2, 2, 1])
        self.assertEqual([result.item.path.name for result in results], [f"img{index}.jpg" for index in range(5)])


class SettingsPersistenceTests(unittest.TestCase):
    """Settings persistence regression tests."""

    def test_load_settings_defaults_missing_skip_backup_to_false(self) -> None:
        """Old settings files without skip_backup must still load with the default off."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_file = root / "settings.json"
            settings_file.write_text(
                json.dumps(
                    {
                        "recursive": False,
                        "table_font_size": 12,
                        "videos": {
                            "fhd_crf": 24,
                            "uhd_crf": 30,
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch("vmp.core.settings.settings_path", return_value=settings_file), patch(
                "vmp.core.settings.fallback_settings_path",
                return_value=root / "fallback.json",
            ):
                settings = load_settings()
        self.assertFalse(settings.skip_backup)
        self.assertEqual(settings.table_font_size, 12)
        self.assertEqual(settings.videos.fhd_crf, 24)
        self.assertEqual(settings.videos.qhd_crf, 26)
        self.assertEqual(settings.videos.uhd_crf, 30)
        self.assertEqual(settings.videos.qhd_long_edge_threshold, 2000)
        self.assertEqual(settings.videos.uhd_long_edge_threshold, 3000)
        self.assertEqual(settings.lasso_thumbnail_workers, 8)
        self.assertEqual(settings.exiftool_read_batch_size, 20)
        self.assertEqual(settings.exiftool_parallel_batches, 1)
        self.assertEqual(settings.folder_drop_behavior, "ask")

    def test_skip_backup_round_trips_through_save_and_load(self) -> None:
        """The skip-backup toggle must persist and restore through the JSON settings file."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_file = root / "settings.json"
            with patch("vmp.core.settings.settings_dir", return_value=root), patch(
                "vmp.core.settings.settings_path",
                return_value=settings_file,
            ), patch(
                "vmp.core.settings.fallback_settings_path",
                return_value=root / "fallback.json",
            ):
                original = AppSettings(skip_backup=True, table_font_size=13)
                save_settings(original)
                loaded = load_settings()
        self.assertTrue(loaded.skip_backup)
        self.assertEqual(loaded.table_font_size, 13)

    def test_diff_tools_and_after_exif_round_trip_through_save_and_load(self) -> None:
        """The new after-EXIF and diff-tool settings must persist through JSON."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_file = root / "settings.json"
            with patch("vmp.core.settings.settings_dir", return_value=root), patch(
                "vmp.core.settings.settings_path",
                return_value=settings_file,
            ), patch(
                "vmp.core.settings.fallback_settings_path",
                return_value=root / "fallback.json",
            ):
                original = AppSettings(
                    skip_backup=False,
                    read_after_exif=True,
                )
                original.videos.limit_to_fhd = True
                original.videos.limit_to_30_fps = True
                original.metadata.set_filesystem_dates = False
                original.diff_tools.image = '"imgdiff.exe" $source $target'
                original.diff_tools.video = '"viddiff.exe" $source $target'
                original.diff_tools.text = '"textdiff.exe" $source $target'
                save_settings(original)
                loaded = load_settings()
        self.assertTrue(loaded.read_after_exif)
        self.assertTrue(loaded.videos.limit_to_fhd)
        self.assertTrue(loaded.videos.limit_to_30_fps)
        self.assertFalse(loaded.metadata.set_filesystem_dates)
        self.assertEqual(loaded.diff_tools.image, '"imgdiff.exe" $source $target')
        self.assertEqual(loaded.diff_tools.video, '"viddiff.exe" $source $target')
        self.assertEqual(loaded.diff_tools.text, '"textdiff.exe" $source $target')

    def test_lasso_settings_round_trip_through_save_and_load(self) -> None:
        """Trip Lasso workflow/cache settings must persist through JSON."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_file = root / "settings.json"
            with patch("vmp.core.settings.settings_dir", return_value=root), patch(
                "vmp.core.settings.settings_path",
                return_value=settings_file,
            ), patch(
                "vmp.core.settings.fallback_settings_path",
                return_value=root / "fallback.json",
            ):
                original = AppSettings()
                original.lasso_load_target_after_move = True
                original.lasso_thumbnail_cache_mode = "disk"
                original.lasso_thumbnail_workers = 6
                original.lasso_thumbnail_display_size = 180
                original.images.parallel_workers = 12
                original.exiftool_read_batch_size = 33
                original.exiftool_parallel_batches = 4
                original.folder_drop_behavior = "add"

                save_settings(original)
                loaded = load_settings()

        self.assertTrue(loaded.lasso_load_target_after_move)
        self.assertEqual(loaded.lasso_thumbnail_cache_mode, "disk")
        self.assertEqual(loaded.lasso_thumbnail_workers, 6)
        self.assertEqual(loaded.lasso_thumbnail_display_size, 180)
        self.assertEqual(loaded.images.parallel_workers, 12)
        self.assertEqual(loaded.exiftool_read_batch_size, 33)
        self.assertEqual(loaded.exiftool_parallel_batches, 4)
        self.assertEqual(loaded.folder_drop_behavior, "add")

    def test_parallel_worker_settings_are_clamped_on_load(self) -> None:
        """Persisted worker counts must stay inside safe ranges."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_file = root / "settings.json"
            settings_file.write_text(
                json.dumps(
                    {
                        "lasso_thumbnail_workers": 99,
                        "exiftool_read_batch_size": 0,
                        "exiftool_parallel_batches": 99,
                        "images": {"parallel_workers": 0},
                    }
                ),
                encoding="utf-8",
            )
            with patch("vmp.core.settings.settings_path", return_value=settings_file), patch(
                "vmp.core.settings.fallback_settings_path",
                return_value=root / "fallback.json",
            ):
                loaded = load_settings()

        self.assertEqual(loaded.lasso_thumbnail_workers, 12)
        self.assertEqual(loaded.images.parallel_workers, 1)
        self.assertEqual(loaded.exiftool_read_batch_size, 1)
        self.assertEqual(loaded.exiftool_parallel_batches, 8)


class CommandTemplateTests(unittest.TestCase):
    """External tool template regression tests."""

    def test_expand_command_template_substitutes_source_and_target(self) -> None:
        """The diff template helper must substitute both placeholders."""
        args = expand_command_template(
            '"tool.exe" --left "$source" --right "$target"',
            source=Path("backup file.jpg"),
            target=Path("current file.jpg"),
        )
        self.assertEqual(args[0], "tool.exe")
        self.assertIn("backup file.jpg", args)
        self.assertIn("current file.jpg", args)

    def test_command_template_rejects_directory_as_executable(self) -> None:
        """A tool folder is not a runnable diff command."""
        with tempfile.TemporaryDirectory() as tmp:
            error = command_template_error(f'"{tmp}" "$source" "$target"')
        self.assertIsNotNone(error)
        self.assertIn("Ordner", str(error))

    def test_launch_command_template_uses_executable_parent_as_cwd(self) -> None:
        """Portable GUI tools should start from their own directory so adjacent DLLs are found."""
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "video-compare.exe"
            exe.write_bytes(b"fake")
            with patch("vmp.core.processes.subprocess.Popen") as popen:
                popen.return_value.pid = 123
                popen.return_value.wait.side_effect = subprocess.TimeoutExpired(cmd=str(exe), timeout=2.0)
                launch_command_template(f'"{exe}" -W "$source" "$target"', source=Path("left.mp4"), target=Path("right.mp4"))

        args, kwargs = popen.call_args
        self.assertEqual(Path(args[0][0]).resolve(), exe.resolve())
        self.assertEqual(Path(kwargs["cwd"]).resolve(), exe.parent.resolve())


class ToolStartupTests(unittest.TestCase):
    """Startup tool-check regression tests."""

    def test_missing_pipeline_tools_checks_only_required_paths(self) -> None:
        """Startup checks must ignore optional helper launchers."""
        settings = AppSettings()
        settings.tools.exiftool = "exiftool-bin"
        settings.tools.ffmpeg = "ffmpeg-bin"
        settings.tools.ffprobe = "ffprobe-bin"
        settings.tools.xnconvert = "nconvert-bin"
        settings.tools.xnconvert_gui = "xnconvert-gui"
        settings.tools.xnviewmp = "xnviewmp"
        settings.tools.shutter_encoder = "shutter-encoder"
        seen: list[str] = []

        def fake_resolve(value: str) -> str | None:
            seen.append(value)
            return None

        with patch("vmp.gui.main.window.resolve_executable", side_effect=fake_resolve):
            missing = _missing_pipeline_tools(settings)

        self.assertEqual(
            seen,
            ["exiftool-bin", "ffmpeg-bin", "ffprobe-bin", "nconvert-bin"],
        )
        self.assertEqual(
            missing,
            [
                ("ExifTool", "exiftool-bin"),
                ("FFmpeg", "ffmpeg-bin"),
                ("FFprobe", "ffprobe-bin"),
                ("NConvert", "nconvert-bin"),
            ],
        )


class SettingsDialogTests(unittest.TestCase):
    """Settings dialog validation regression tests."""

    def test_diff_tool_template_directory_shows_error_and_does_not_save(self) -> None:
        """Saving should reject a diff template whose command points at a folder."""
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        with tempfile.TemporaryDirectory() as tmp:
            settings = AppSettings()
            settings.diff_tools.video = f'"{tmp}" "$source" "$target"'
            dialog = SettingsDialog(settings)
            with patch("vmp.gui.settings_dialog.QMessageBox.warning") as warning, patch(
                "vmp.gui.settings_dialog.save_settings"
            ) as save_settings_mock:
                dialog._on_save()

        warning.assert_called_once()
        save_settings_mock.assert_not_called()

    def test_surface_settings_save_parallel_worker_values(self) -> None:
        """The settings dialog should persist UI/parallelism settings into AppSettings."""
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        settings = AppSettings()
        with patch("vmp.gui.settings_dialog.save_settings") as save_settings_mock:
            dialog = SettingsDialog(settings)
            dialog.table_font_spin.setValue(14)
            dialog.image_workers_spin.setValue(11)
            dialog.lasso_workers_spin.setValue(5)
            dialog.exiftool_batch_size_spin.setValue(37)
            dialog.exiftool_parallel_batches_spin.setValue(3)
            dialog.lasso_cache_combo.setCurrentIndex(dialog.lasso_cache_combo.findData("disk"))
            dialog.folder_drop_combo.setCurrentIndex(dialog.folder_drop_combo.findData("replace"))

            dialog._on_save()

        self.assertEqual(settings.table_font_size, 14)
        self.assertEqual(settings.images.parallel_workers, 11)
        self.assertEqual(settings.lasso_thumbnail_workers, 5)
        self.assertEqual(settings.exiftool_read_batch_size, 37)
        self.assertEqual(settings.exiftool_parallel_batches, 3)
        self.assertEqual(settings.lasso_thumbnail_cache_mode, "disk")
        self.assertEqual(settings.folder_drop_behavior, "replace")
        save_settings_mock.assert_called_once_with(settings)


class MainWindowCloseEventTests(unittest.TestCase):
    """GUI close-path regression tests."""

    def test_close_event_syncs_sidebar_settings_before_save(self) -> None:
        """Closing the main window must persist current right-sidebar state."""
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        with patch("vmp.gui.main.window.load_settings", return_value=AppSettings()), patch(
            "vmp.gui.main.window.configure_logging",
            return_value=Path("log.txt"),
        ), patch(
            "vmp.gui.main.window.setup_gui_logging"
        ), patch(
            "vmp.gui.main.window.resolve_executable",
            return_value="tool",
        ), patch(
            "vmp.gui.main.window.save_settings"
        ) as save_settings:
            window = MainWindow()
            window.skip_backup_check.setChecked(True)
            window.recursive_check.setChecked(False)
            window.read_after_exif_check.setChecked(True)
            window.filesystem_dates_check.setChecked(False)
            window.quality_spin.setValue(91)
            event = QCloseEvent()
            window.closeEvent(event)

        self.assertTrue(window.settings_model.skip_backup)
        self.assertFalse(window.settings_model.recursive)
        self.assertTrue(window.settings_model.read_after_exif)
        self.assertFalse(window.settings_model.metadata.set_filesystem_dates)
        self.assertEqual(window.settings_model.images.jpeg_quality, 91)
        save_settings.assert_called_once()


class MainWindowPostApplyTests(unittest.TestCase):
    """GUI regression tests for post-apply state updates."""

    def test_only_applied_plans_are_marked_done(self) -> None:
        """Image-only apply updates must not mark untouched video plans as DONE."""
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        with patch("vmp.gui.main.window.load_settings", return_value=AppSettings()), patch(
            "vmp.gui.main.window.configure_logging",
            return_value=Path("log.txt"),
        ), patch(
            "vmp.gui.main.window.setup_gui_logging"
        ), patch(
            "vmp.gui.main.window.resolve_executable",
            return_value="tool",
        ), patch(
            "vmp.gui.main.window.save_settings"
        ):
            window = MainWindow()

        window.populate_table = lambda: None  # type: ignore[assignment]
        window._set_busy = lambda *_args, **_kwargs: None  # type: ignore[assignment]

        root = Path.cwd()
        image_source = root / "image.jpg"
        video_source = root / "video.mp4"
        image_plan = MediaPlan(
            analysis=AnalysisResult(
                item=MediaItem(image_source, root, MediaKind.IMAGE),
                metadata=RawMetadata(str(image_source), {}),
                resolved=ResolvedTimestamp(None, None, None, Confidence.LOW),
                status=PlanStatus.OK,
            ),
            final_path=root / "image-renamed.jpg",
        )
        video_plan = MediaPlan(
            analysis=AnalysisResult(
                item=MediaItem(video_source, root, MediaKind.VIDEO),
                metadata=RawMetadata(str(video_source), {}),
                resolved=ResolvedTimestamp(None, None, None, Confidence.LOW),
                status=PlanStatus.OK,
            ),
            final_path=root / "video-renamed.mp4",
        )
        window.plans = [image_plan, video_plan]
        window._original_sizes = {image_source: 100, video_source: 200}
        window._applied_plans = [image_plan]

        window._update_plan_references_after_apply()

        self.assertEqual(image_plan.analysis.status, PlanStatus.DONE)
        self.assertEqual(image_plan.analysis.item.path, root / "image-renamed.jpg")
        self.assertEqual(video_plan.analysis.status, PlanStatus.OK)
        self.assertEqual(video_plan.analysis.item.path, video_source)

    def test_stats_show_delta_only_for_applied_category(self) -> None:
        """The status bar should show a before/after delta only for touched media kinds."""
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        with patch("vmp.gui.main.window.load_settings", return_value=AppSettings()), patch(
            "vmp.gui.main.window.configure_logging",
            return_value=Path("log.txt"),
        ), patch(
            "vmp.gui.main.window.setup_gui_logging"
        ), patch(
            "vmp.gui.main.window.resolve_executable",
            return_value="tool",
        ), patch(
            "vmp.gui.main.window.save_settings"
        ):
            window = MainWindow()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_path = root / "photo.jpg"
            video_path = root / "clip.mp4"
            image_path.write_bytes(b"a" * 80)
            video_path.write_bytes(b"b" * 200)
            image_plan = MediaPlan(
                analysis=AnalysisResult(
                    item=MediaItem(image_path, root, MediaKind.IMAGE),
                    metadata=RawMetadata(str(image_path), {}),
                    resolved=ResolvedTimestamp(None, None, None, Confidence.LOW),
                    status=PlanStatus.OK,
                ),
                final_path=image_path,
            )
            video_plan = MediaPlan(
                analysis=AnalysisResult(
                    item=MediaItem(video_path, root, MediaKind.VIDEO),
                    metadata=RawMetadata(str(video_path), {}),
                    resolved=ResolvedTimestamp(None, None, None, Confidence.LOW),
                    status=PlanStatus.OK,
                ),
                final_path=video_path,
            )
            window.plans = [image_plan, video_plan]
            window._original_sizes = {image_path: 100}

            window._update_stats()

            stats = window.stats_label.text()

        self.assertIn("Bilder: 1 (100 B -> 80 B)", stats)
        self.assertIn("Videos: 1 (200 B)", stats)
        self.assertIn("Gesamt: 2 (300 B -> 280 B)", stats)

    def test_remove_active_paths_drops_results_plans_and_updates_stats(self) -> None:
        """Removing rows from the table must remove them from the active pipeline state."""
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        with patch("vmp.gui.main.window.load_settings", return_value=AppSettings()), patch(
            "vmp.gui.main.window.configure_logging",
            return_value=Path("log.txt"),
        ), patch(
            "vmp.gui.main.window.setup_gui_logging"
        ), patch(
            "vmp.gui.main.window.resolve_executable",
            return_value="tool",
        ), patch(
            "vmp.gui.main.window.save_settings"
        ):
            window = MainWindow()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_path = root / "IMG_20240101_120000.jpg"
            video_path = root / "VID_20240101_120000.mp4"
            image_path.write_bytes(b"a" * 80)
            video_path.write_bytes(b"b" * 200)
            img_result = AnalysisResult(
                item=MediaItem(image_path, root, MediaKind.IMAGE),
                metadata=RawMetadata(str(image_path), {"EXIF:DateTimeOriginal": "2024:01:01 12:00:00"}),
                resolved=ResolvedTimestamp(datetime(2024, 1, 1, 12, 0, 0), None, None, Confidence.HIGH),
                status=PlanStatus.OK,
            )
            vid_result = AnalysisResult(
                item=MediaItem(video_path, root, MediaKind.VIDEO),
                metadata=RawMetadata(str(video_path), {"QuickTime:CreateDate": "2024:01:01 11:00:00"}),
                resolved=ResolvedTimestamp(datetime(2024, 1, 1, 12, 0, 0), None, None, Confidence.HIGH),
                status=PlanStatus.OK,
            )
            window.results = [img_result, vid_result]
            window.plans = [
                MediaPlan(img_result, final_path=image_path),
                MediaPlan(vid_result, final_path=video_path),
            ]
            window.populate_table()

            removed = window._remove_active_paths([image_path], "Test")

        self.assertEqual(removed, 1)
        self.assertEqual([result.item.path for result in window.results], [video_path])
        self.assertEqual([plan.analysis.item.path for plan in window.plans], [video_path])
        self.assertEqual(window.table.rowCount(), 1)
        self.assertIn("Bilder: 0", window.stats_label.text())
        self.assertIn("Videos: 1", window.stats_label.text())

    def test_remove_selected_rows_ignores_hidden_filtered_rows(self) -> None:
        """Ctrl+A/range selections with filters must act only on visible rows."""
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        with patch("vmp.gui.main.window.load_settings", return_value=AppSettings()), patch(
            "vmp.gui.main.window.configure_logging",
            return_value=Path("log.txt"),
        ), patch(
            "vmp.gui.main.window.setup_gui_logging"
        ), patch(
            "vmp.gui.main.window.resolve_executable",
            return_value="tool",
        ), patch(
            "vmp.gui.main.window.save_settings"
        ):
            window = MainWindow()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [root / f"IMG_20240101_12000{idx}.dat" for idx in range(3)]
            for path in paths:
                path.write_bytes(b"data")
            results = [
                AnalysisResult(
                    item=MediaItem(path, root, MediaKind.IMAGE),
                    metadata=RawMetadata(str(path), {"EXIF:DateTimeOriginal": "2024:01:01 12:00:00"}),
                    resolved=ResolvedTimestamp(datetime(2024, 1, 1, 12, 0, 0), None, None, Confidence.HIGH),
                    status=PlanStatus.OK,
                )
                for path in paths
            ]
            window.results = results
            window.plans = [MediaPlan(result, final_path=result.item.path) for result in results]
            window.populate_table()
            window.table.setRowHidden(1, True)
            window.table.selectAll()

            with patch(
                "vmp.gui.main.window.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                window.remove_selected_rows_from_table()

        self.assertEqual([result.item.path for result in window.results], [paths[1]])
        self.assertEqual(window.table.rowCount(), 1)
        self.assertIn("Bilder: 1", window.stats_label.text())

    def test_workflow_refresh_is_debounced_for_repeated_sidebar_changes(self) -> None:
        """Repeated sidebar edits should restart one timer instead of rebuilding immediately."""
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        with patch("vmp.gui.main.window.load_settings", return_value=AppSettings()), patch(
            "vmp.gui.main.window.configure_logging",
            return_value=Path("log.txt"),
        ), patch(
            "vmp.gui.main.window.setup_gui_logging"
        ), patch(
            "vmp.gui.main.window.resolve_executable",
            return_value="tool",
        ), patch(
            "vmp.gui.main.window.save_settings"
        ):
            window = MainWindow()
        with patch.object(window._workflow_refresh_timer, "start") as start:
            window._schedule_workflow_refresh()
            window._schedule_workflow_refresh()
            window._schedule_workflow_refresh()
        self.assertEqual(start.call_count, 3)

    def test_workflow_refresh_updates_only_workflow_columns_when_row_count_matches(self) -> None:
        """Sidebar changes should not rebuild every table cell when rows are stable."""
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        with patch("vmp.gui.main.window.load_settings", return_value=AppSettings()), patch(
            "vmp.gui.main.window.configure_logging",
            return_value=Path("log.txt"),
        ), patch(
            "vmp.gui.main.window.setup_gui_logging"
        ), patch(
            "vmp.gui.main.window.resolve_executable",
            return_value="tool",
        ), patch(
            "vmp.gui.main.window.save_settings"
        ):
            window = MainWindow()
        path = Path.cwd() / "photo.jpg"
        result = AnalysisResult(
            item=MediaItem(path, Path.cwd(), MediaKind.IMAGE),
            metadata=RawMetadata(str(path), {}),
            resolved=ResolvedTimestamp(None, None, None, Confidence.LOW),
            status=PlanStatus.OK,
        )
        plan = MediaPlan(analysis=result, final_path=path)
        window.results = [result]
        window.table.setRowCount(1)
        with patch("vmp.gui.main.window.build_plans", return_value=[plan]), patch.object(
            window, "populate_table"
        ) as populate_table, patch.object(
            window, "_refresh_workflow_table_columns"
        ) as refresh_workflow_columns:
            window._refresh_plans_from_results()

        populate_table.assert_not_called()
        refresh_workflow_columns.assert_called_once()

    def test_tolerance_change_reanalyzes_existing_scan_results(self) -> None:
        """The live tolerance control must update WARN/OK decisions without a rescan."""
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        with patch("vmp.gui.main.window.load_settings", return_value=AppSettings()), patch(
            "vmp.gui.main.window.configure_logging", return_value=Path("log.txt")
        ), patch("vmp.gui.main.window.setup_gui_logging"), patch(
            "vmp.gui.main.window.resolve_executable", return_value="tool"
        ), patch("vmp.gui.main.window.save_settings"):
            window = MainWindow()
        root = Path.cwd()
        item = MediaItem(root / "photo.jpg", root, MediaKind.IMAGE)
        raw = RawMetadata(
            str(item.path),
            {
                "EXIF:DateTimeOriginal": "2026:01:02 03:04:05",
                "EXIF:CreateDate": "2026:01:02 03:05:05",
            },
        )
        initial = analyze_item(item, raw, window.settings_model.metadata)
        self.assertEqual(initial.status, PlanStatus.OK)
        window.results = [initial]

        window.tolerance_spin.setValue(0)
        window._apply_workflow_refresh()

        self.assertEqual(window.results[0].status, PlanStatus.WARN)
        self.assertTrue(any("Conflicting local" in warning for warning in window.results[0].warnings))

    def test_apply_flushes_debounced_workflow_plan_before_starting_worker(self) -> None:
        """Apply must use the current sidebar settings, never the stale pre-debounce plan."""
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        with tempfile.TemporaryDirectory() as tmp, patch(
            "vmp.gui.main.window.load_settings", return_value=AppSettings()
        ), patch(
            "vmp.gui.main.window.configure_logging", return_value=Path("log.txt")
        ), patch(
            "vmp.gui.main.window.setup_gui_logging"
        ), patch(
            "vmp.gui.main.window.resolve_executable", return_value="tool"
        ), patch(
            "vmp.gui.main.window.save_settings"
        ):
            root = Path(tmp)
            source = root / "photo.heic"
            source.write_bytes(b"image")
            result = AnalysisResult(
                item=MediaItem(source, root, MediaKind.IMAGE),
                metadata=RawMetadata(str(source), {}),
                resolved=ResolvedTimestamp(datetime(2026, 1, 2, 3, 4, 5), None, None, Confidence.HIGH),
                status=PlanStatus.OK,
            )
            stale = MediaPlan(
                analysis=result,
                actions=[PlannedAction(ActionKind.IMAGE_CONVERT, "stale conversion", source)],
                final_path=root / "20260102_030405.jpg",
            )
            current = MediaPlan(
                analysis=result,
                actions=[PlannedAction(ActionKind.METADATA_NORMALIZE, "current metadata", source)],
                final_path=root / "20260102_030405.heic",
            )
            window = MainWindow()
            window.roots = [root]
            window.results = [result]
            window.plans = [stale]

            def install_current_plan() -> None:
                window.plans = [current]

            with patch.object(
                window, "_refresh_plans_from_results", side_effect=install_current_plan
            ) as refresh, patch.object(
                window, "_start_worker"
            ) as start_worker, patch(
                "vmp.gui.main.apply_flow.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                window.run_plans([stale], "Apply?")

        refresh.assert_called_once()
        worker = start_worker.call_args.args[0]
        self.assertEqual(worker._plans, [current])
        self.assertEqual(window._applied_plans, [current])

    def test_failed_apply_update_keeps_gui_on_existing_source_path(self) -> None:
        """A failed target must not replace the GUI's valid source reference."""
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        with tempfile.TemporaryDirectory() as tmp, patch(
            "vmp.gui.main.window.load_settings", return_value=AppSettings()
        ), patch(
            "vmp.gui.main.window.configure_logging", return_value=Path("log.txt")
        ), patch(
            "vmp.gui.main.window.setup_gui_logging"
        ), patch(
            "vmp.gui.main.window.resolve_executable", return_value="tool"
        ), patch(
            "vmp.gui.main.window.save_settings"
        ):
            root = Path(tmp)
            source = root / "photo.jpg"
            target = root / "20260102_030405.jpg"
            source.write_bytes(b"source")
            result = AnalysisResult(
                item=MediaItem(source, root, MediaKind.IMAGE),
                metadata=RawMetadata(str(source), {}),
                resolved=ResolvedTimestamp(None, None, None, Confidence.LOW),
                status=PlanStatus.OK,
            )
            window = MainWindow()
            window.results = [result]
            window.plans = [MediaPlan(analysis=result, final_path=target)]
            window.populate_table()
            window.on_apply_item_updated(
                ApplyItemUpdate(
                    run_id="run",
                    source_path=source,
                    final_path=None,
                    skipped=True,
                    errors=["target appeared"],
                )
            )

        self.assertEqual(window.plans[0].final_path, source)
        self.assertEqual(window.plans[0].analysis.item.path, source)
        self.assertEqual(window.plans[0].analysis.status, PlanStatus.SKIP)

    def test_diff_backup_is_discovered_from_manifest_when_memory_map_is_empty(self) -> None:
        """Diff actions should remain available after a rescan/restart when a backup exists."""
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "clip.mov"
            current = root / "20260606_105018.mp4"
            backup = root / "_VacationMediaProcessor_Backup" / "20260606_110000" / "clip.mov"
            manifest = root / "_VacationMediaProcessor_Manifest" / "20260606_110000_after.json"
            current.write_bytes(b"current")
            backup.parent.mkdir(parents=True)
            backup.write_bytes(b"backup")
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps(
                    {
                        "files": {
                            "clip.mov": {
                                "original_path": str(original),
                                "file_path": str(current),
                                "final_path": str(current),
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch("vmp.gui.main.window.load_settings", return_value=AppSettings()), patch(
                "vmp.gui.main.window.configure_logging",
                return_value=Path("log.txt"),
            ), patch(
                "vmp.gui.main.window.setup_gui_logging"
            ), patch(
                "vmp.gui.main.window.resolve_executable",
                return_value="tool",
            ), patch(
                "vmp.gui.main.window.save_settings"
            ):
                window = MainWindow()
            window.settings_model.skip_backup = True
            plan = MediaPlan(
                analysis=AnalysisResult(
                    item=MediaItem(current, root, MediaKind.VIDEO),
                    metadata=RawMetadata(str(current), {}),
                    resolved=ResolvedTimestamp(None, None, None, Confidence.LOW),
                    status=PlanStatus.DONE,
                ),
                final_path=current,
            )

            discovered = window._backup_path_for_plan(plan)
            can_open = window._can_open_diff_for_plan(plan)

            self.assertIsNotNone(discovered)
            self.assertEqual(discovered.resolve(), backup.resolve())
            self.assertTrue(can_open)

    def test_apply_item_update_refreshes_only_finished_row_and_enables_diff(self) -> None:
        """A per-file apply update should patch the row and expose diff only after DONE."""
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "photo.heic"
            final = root / "20260425_191502.jpg"
            backup = root / "_VacationMediaProcessor_Backup" / "20260606_110000" / "photo.heic"
            source.write_bytes(b"original")
            final.write_bytes(b"converted-jpeg")
            backup.parent.mkdir(parents=True)
            backup.write_bytes(b"backup")
            settings = AppSettings()
            settings.diff_tools.image = '"diff.exe" "$source" "$target"'
            with patch("vmp.gui.main.window.load_settings", return_value=settings), patch(
                "vmp.gui.main.window.configure_logging",
                return_value=Path("log.txt"),
            ), patch(
                "vmp.gui.main.window.setup_gui_logging"
            ), patch(
                "vmp.gui.main.window.resolve_executable",
                return_value="tool",
            ), patch(
                "vmp.gui.main.window.save_settings"
            ):
                window = MainWindow()
            plan = MediaPlan(
                analysis=AnalysisResult(
                    item=MediaItem(source, root, MediaKind.IMAGE),
                    metadata=RawMetadata(str(source), {}),
                    resolved=ResolvedTimestamp(None, None, None, Confidence.LOW),
                    status=PlanStatus.OK,
                ),
                final_path=final,
            )
            window.plans = [plan]
            window.populate_table()

            self.assertFalse(window._can_open_diff_for_plan(plan))
            with patch.object(window, "populate_table") as populate_table:
                window.on_apply_item_updated(
                    ApplyItemUpdate(
                        run_id="20260606_110000",
                        source_path=source,
                        final_path=final,
                        backup_path=backup,
                        changed=True,
                        original_size=100,
                        current_size=14,
                    )
                )

            populate_table.assert_not_called()
            self.assertEqual(plan.analysis.status, PlanStatus.DONE)
            self.assertEqual(plan.analysis.item.path, final)
            self.assertEqual(window.table.item(0, 0).text(), "DONE")
            self.assertEqual(window.table.item(0, 2).text(), "20260425_191502.jpg")
            self.assertTrue(window._can_open_diff_for_plan(plan))

    def test_readback_diff_button_opens_before_after_json_with_textdiff(self) -> None:
        """The post-readback diff button should launch Textdiff with before/after JSONs."""
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before_json = root / "_VacationMediaProcessor_Manifest" / "20260606_110000_before.json"
            after_json = root / "_VacationMediaProcessor_Manifest" / "20260606_110000_after.json"
            before_json.parent.mkdir(parents=True)
            before_json.write_text("{}", encoding="utf-8")
            after_json.write_text("{}", encoding="utf-8")
            settings = AppSettings()
            settings.diff_tools.text = '"diff.exe" "$source" "$target"'
            with patch("vmp.gui.main.window.load_settings", return_value=settings), patch(
                "vmp.gui.main.window.configure_logging",
                return_value=Path("log.txt"),
            ), patch(
                "vmp.gui.main.window.setup_gui_logging"
            ), patch(
                "vmp.gui.main.window.resolve_executable",
                return_value="tool",
            ), patch(
                "vmp.gui.main.window.save_settings"
            ), patch(
                "vmp.gui.main.diff_actions.launch_command_template"
            ) as launch_command_template:
                window = MainWindow()
                window.roots = [root]

                paths = window._readback_manifest_paths_for_run("20260606_110000")
                window._set_readback_diff_paths(paths)
                window.open_readback_diff()

        expected_paths = (before_json.resolve(), after_json.resolve())
        self.assertEqual(paths, expected_paths)
        self.assertTrue(window.readback_diff_button.isEnabled())
        launch_command_template.assert_called_once_with(
            settings.diff_tools.text,
            source=expected_paths[0],
            target=expected_paths[1],
        )

    def test_row_textdiff_reads_fresh_exif_snapshots_then_launches_textdiff(self) -> None:
        """The row textdiff action should compare fresh ExifTool JSON for backup/current files."""
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup = root / "_VacationMediaProcessor_Backup" / "20260606_110000" / "clip.mov"
            current = root / "clip.mp4"
            backup.parent.mkdir(parents=True)
            backup.write_bytes(b"backup")
            current.write_bytes(b"current")
            settings = AppSettings()
            settings.diff_tools.text = '"textdiff.exe" "$source" "$target"'
            with patch("vmp.gui.main.window.load_settings", return_value=settings), patch(
                "vmp.gui.main.window.configure_logging",
                return_value=Path("log.txt"),
            ), patch(
                "vmp.gui.main.window.setup_gui_logging"
            ), patch(
                "vmp.gui.main.window.resolve_executable",
                return_value="tool",
            ), patch(
                "vmp.gui.main.window.save_settings"
            ), patch(
                "vmp.gui.main.diff_actions.run_process",
                side_effect=[
                    type("Result", (), {"stdout": '[{"ZZZ:LastTag":"last","AAA:FirstTag":"first","SourceFile":"backup"}]'})(),
                    type("Result", (), {"stdout": '[{"ZZZ:LastTag":"last","AAA:FirstTag":"first","SourceFile":"current"}]'})(),
                ],
            ) as run_process, patch(
                "vmp.gui.main.diff_actions.launch_command_template"
            ) as launch_command_template:
                window = MainWindow()
                plan = MediaPlan(
                    analysis=AnalysisResult(
                        item=MediaItem(current, root, MediaKind.VIDEO),
                        metadata=RawMetadata(str(current), {}),
                        resolved=ResolvedTimestamp(None, None, None, Confidence.LOW),
                        status=PlanStatus.DONE,
                    ),
                    final_path=current,
                )
                window.plans = [plan]
                window._backup_paths[current.resolve()] = backup

                opened = window._open_exif_text_diff_for_row(0)

                self.assertTrue(opened)
                self.assertEqual(run_process.call_count, 2)
                args_before = run_process.call_args_list[0].args[0]
                args_after = run_process.call_args_list[1].args[0]
                self.assertIn("-all:all", args_before)
                self.assertEqual(args_before[-1], str(backup))
                self.assertEqual(args_after[-1], str(current))
                launch_command_template.assert_called_once()
                _, kwargs = launch_command_template.call_args
                self.assertTrue(kwargs["source"].name.endswith("_before_exif.json"))
                self.assertTrue(kwargs["target"].name.endswith("_after_exif.json"))
                self.assertTrue(kwargs["source"].exists())
                self.assertTrue(kwargs["target"].exists())
                before_text = kwargs["source"].read_text(encoding="utf-8")
                self.assertLess(before_text.index('"AAA:FirstTag"'), before_text.index('"SourceFile"'))
                self.assertLess(before_text.index('"SourceFile"'), before_text.index('"ZZZ:LastTag"'))


class ManifestSnapshotTests(unittest.TestCase):
    """Before/after manifest regression tests."""

    def test_before_after_manifests_share_original_filename_keys_and_entry_shape(self) -> None:
        """Before/after manifests should be easy to compare side by side in diff tools."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before_file = root / "IMG_0001.heic"
            after_file = root / "20260425_191502.jpg"
            before_file.write_bytes(b"before")
            after_file.write_bytes(b"after")
            item = MediaItem(before_file, root, MediaKind.IMAGE)
            analysis = AnalysisResult(
                item=item,
                metadata=RawMetadata(
                    str(before_file),
                    {
                        "ZZZ:LastTag": "last",
                        "AAA:FirstTag": "first",
                        "EXIF:DateTimeOriginal": "2026:04:25 19:15:02",
                    },
                ),
                resolved=ResolvedTimestamp(None, None, None, Confidence.HIGH),
                status=PlanStatus.OK,
            )
            plan = MediaPlan(analysis=analysis, final_path=after_file)
            after_metadata = {
                after_file.resolve(): RawMetadata(
                    str(after_file),
                    {
                        "ZZZ:LastTag": "last",
                        "AAA:FirstTag": "first",
                        "EXIF:DateTimeOriginal": "2026:04:25 19:15:02",
                    },
                )
            }
            before_manifest = root / "run_before.json"
            after_manifest = root / "run_after.json"

            write_before_after_manifests(
                before_manifest,
                after_manifest,
                root=root,
                settings=AppSettings(),
                plans=[plan],
                report=None,
                after_metadata=after_metadata,
            )

            before_payload = json.loads(before_manifest.read_text(encoding="utf-8"))
            after_payload = json.loads(after_manifest.read_text(encoding="utf-8"))
            before_text = before_manifest.read_text(encoding="utf-8")

        self.assertEqual(before_payload["files"].keys(), after_payload["files"].keys())
        self.assertIn("IMG_0001.heic", before_payload["files"])
        self.assertNotIn("IMG_0001.heic -> 20260425_191502.jpg", before_payload["files"])
        before_entry = before_payload["files"]["IMG_0001.heic"]
        after_entry = after_payload["files"]["IMG_0001.heic"]
        self.assertEqual(before_entry.keys(), after_entry.keys())
        self.assertEqual(before_entry["file_path"], str(before_file))
        self.assertEqual(after_entry["file_path"], str(after_file))
        self.assertEqual(before_entry["metadata"]["source_file"], str(before_file))
        self.assertEqual(after_entry["metadata"]["source_file"], str(after_file))
        self.assertLess(before_text.index('"AAA:FirstTag"'), before_text.index('"EXIF:DateTimeOriginal"'))
        self.assertLess(before_text.index('"EXIF:DateTimeOriginal"'), before_text.index('"ZZZ:LastTag"'))


class ApplyParallelismTests(unittest.TestCase):
    """Apply orchestration regression tests."""

    def _make_plan(self, root: Path, name: str, kind: MediaKind) -> MediaPlan:
        source = root / name
        item = MediaItem(source, root, kind)
        analysis = analyze_item(
            item,
            RawMetadata(str(source), {}),
            AppSettings().metadata,
        )
        analysis.status = PlanStatus.OK
        analysis.resolved = ResolvedTimestamp(None, None, None, Confidence.LOW)
        return MediaPlan(analysis=analysis, final_path=source)

    def test_image_plans_use_executor_and_videos_stay_serial(self) -> None:
        """Image plans should go through the bounded pool before serial video work starts."""
        class FakeExecutor:
            instances = []

            def __init__(self, max_workers: int) -> None:
                self.max_workers = max_workers
                self.submitted: list[str] = []
                FakeExecutor.instances.append(self)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def submit(self, fn, *args, **kwargs):
                from concurrent.futures import Future

                plan = args[0]
                self.submitted.append(plan.analysis.item.path.name)
                future = Future()
                future.set_result(fn(*args, **kwargs))
                return future

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_one = self._make_plan(root, "img1.jpg", MediaKind.IMAGE)
            image_two = self._make_plan(root, "img2.jpg", MediaKind.IMAGE)
            video = self._make_plan(root, "clip.mp4", MediaKind.VIDEO)
            settings = AppSettings()
            settings.skip_backup = True
            settings.images.parallel_workers = 8
            call_order: list[str] = []
            item_updates: list[ApplyItemUpdate] = []
            progress_events = []

            def fake_produce_output(plan, *args, **kwargs):
                call_order.append(plan.analysis.item.path.name)
                return plan.analysis.item.path

            with patch("vmp.pipeline.apply.ThreadPoolExecutor", FakeExecutor), patch(
                "vmp.pipeline.apply._preflight_apply_tools"
            ), patch(
                "vmp.pipeline.apply._produce_output",
                side_effect=fake_produce_output,
            ), patch(
                "vmp.pipeline.apply.write_metadata"
            ), patch(
                "vmp.pipeline.apply.copy_all_metadata"
            ), patch(
                "vmp.pipeline.apply._backup_source"
            ) as backup_source, patch(
                "vmp.pipeline.apply.write_manifest"
            ), patch(
                "vmp.pipeline.apply.probe_video",
                return_value={"format": {"duration": 12.0}},
            ):
                report = apply_plans(
                    root,
                    [image_one, image_two, video],
                    settings,
                    callback=progress_events.append,
                    item_callback=item_updates.append,
                )

        self.assertEqual(len(FakeExecutor.instances), 1)
        executor = FakeExecutor.instances[0]
        self.assertEqual(executor.max_workers, 2)
        self.assertEqual(executor.submitted, ["img1.jpg", "img2.jpg"])
        self.assertEqual(call_order, ["img1.jpg", "img2.jpg", "clip.mp4"])
        self.assertEqual(report.changed, 3)
        self.assertCountEqual([update.source_path.name for update in item_updates], ["img1.jpg", "img2.jpg", "clip.mp4"])
        self.assertTrue(all(update.changed for update in item_updates))
        backup_source.assert_not_called()
        self.assertNotIn("video_transcode", [event.phase.value for event in progress_events])
        self.assertNotIn("metadata_write", [event.phase.value for event in progress_events])

    def test_video_progress_uses_only_duration_weighted_transcode_updates(self) -> None:
        """Preparing and metadata work must not replace duration-weighted progress with item counts."""
        from vmp.pipeline.apply import _produce_output

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "clip.mov"
            source.write_bytes(b"source" * 100)
            plan = self._make_plan(root, source.name, MediaKind.VIDEO)
            plan.actions = [PlannedAction(ActionKind.VIDEO_TRANSCODE, "transcode", source)]
            settings = AppSettings()
            events = []

            def fake_transcode(_source, target, _crf, _settings, _downscale, progress_fn):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"output")
                progress_fn(0.0, 10.0, "1.0x")
                progress_fn(5.0, 10.0, "1.0x")
                progress_fn(10.0, 10.0, "1.0x")

            with patch("vmp.pipeline.apply.transcode_video", side_effect=fake_transcode):
                output = _produce_output(
                    plan,
                    root,
                    root / "work",
                    settings,
                    events.append,
                    index=4,
                    total=10,
                    video_state={"completed": 30.0, "speed_samples": []},
                    total_video_seconds=100.0,
                    video_durations={source: 10.0},
                )

        self.assertIsNotNone(output)
        self.assertEqual([event.current for event in events], [30, 35, 40])
        self.assertEqual([event.total for event in events], [100, 100, 100])
        self.assertTrue(all(event.phase.value == "video_transcode" for event in events))

    def test_image_plans_fall_back_to_serial_when_workers_is_one(self) -> None:
        """A worker count of one must keep image work serial and still succeed."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_one = self._make_plan(root, "img1.jpg", MediaKind.IMAGE)
            image_two = self._make_plan(root, "img2.jpg", MediaKind.IMAGE)
            settings = AppSettings()
            settings.skip_backup = False
            settings.images.parallel_workers = 1
            call_order: list[str] = []

            def fake_produce_output(plan, *args, **kwargs):
                call_order.append(plan.analysis.item.path.name)
                return plan.analysis.item.path

            with patch("vmp.pipeline.apply.ThreadPoolExecutor") as executor_mock, patch(
                "vmp.pipeline.apply._preflight_apply_tools"
            ), patch(
                "vmp.pipeline.apply._produce_output",
                side_effect=fake_produce_output,
            ), patch(
                "vmp.pipeline.apply.write_metadata"
            ), patch(
                "vmp.pipeline.apply.copy_all_metadata"
            ), patch(
                "vmp.pipeline.apply._backup_source"
            ) as backup_source, patch(
                "vmp.pipeline.apply.write_manifest"
            ):
                report = apply_plans(root, [image_one, image_two], settings)

        executor_mock.assert_not_called()
        self.assertEqual(call_order, ["img1.jpg", "img2.jpg"])
        self.assertEqual(report.changed, 2)
        self.assertEqual(backup_source.call_count, 2)

    def test_stale_rename_target_is_never_overwritten(self) -> None:
        """A file created after planning must survive a metadata-only rename apply."""
        from vmp.pipeline import _apply_one_plan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "photo.jpg"
            target = root / "20260102_030405.jpg"
            source.write_bytes(b"original source")
            target.write_bytes(b"new unrelated file")
            plan = self._make_plan(root, source.name, MediaKind.IMAGE)
            plan.final_path = target
            settings = AppSettings(skip_backup=True)

            with patch("vmp.pipeline.apply._produce_output") as produce, patch(
                "vmp.pipeline.apply.write_metadata"
            ) as write_metadata:
                outcome = _apply_one_plan(plan, settings, "run", None, 1, 1)

            self.assertFalse(outcome.changed)
            self.assertTrue(outcome.skipped)
            self.assertIn("refusing to overwrite", outcome.errors[0])
            self.assertEqual(source.read_bytes(), b"original source")
            self.assertEqual(target.read_bytes(), b"new unrelated file")
            produce.assert_not_called()
            write_metadata.assert_not_called()

    def test_failed_item_readback_uses_actual_source_not_missing_target(self) -> None:
        """Before/after readback must follow the surviving path after an item failure."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "photo.jpg"
            target = root / "20260102_030405.jpg"
            source.write_bytes(b"original source")
            target.write_bytes(b"new unrelated file")
            plan = self._make_plan(root, source.name, MediaKind.IMAGE)
            plan.final_path = target
            settings = AppSettings(skip_backup=True, read_after_exif=True)
            settings.images.parallel_workers = 1
            read_paths: list[Path] = []

            def capture_readback(items, _exiftool):
                read_paths.extend(item.path for item in items)
                return {}

            with patch("vmp.pipeline.apply._preflight_apply_tools"), patch(
                "vmp.pipeline.apply.write_manifest"
            ), patch(
                "vmp.pipeline.apply.write_before_after_manifests"
            ), patch(
                "vmp.pipeline.apply.read_metadata_batch", side_effect=capture_readback
            ):
                report = apply_plans(root, [plan], settings)

        self.assertEqual(len(report.errors), 1)
        self.assertEqual(read_paths, [source])
        self.assertEqual(plan.final_path, source)

    def test_cancelled_plan_does_not_start_output_or_file_mutation(self) -> None:
        """Queued image tasks should become harmless skips after cancellation."""
        from vmp.pipeline import _apply_one_plan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = self._make_plan(root, "photo.jpg", MediaKind.IMAGE)
            plan.analysis.item.path.write_bytes(b"source")
            with patch("vmp.pipeline.apply._produce_output") as produce, patch(
                "vmp.pipeline.apply._backup_source"
            ) as backup, patch(
                "vmp.pipeline.apply.write_metadata"
            ) as write_metadata:
                outcome = _apply_one_plan(
                    plan,
                    AppSettings(),
                    "run",
                    None,
                    1,
                    1,
                    cancel_callback=lambda: True,
                )

        self.assertTrue(outcome.skipped)
        self.assertFalse(outcome.changed)
        produce.assert_not_called()
        backup.assert_not_called()
        write_metadata.assert_not_called()

    def test_after_exif_readback_failure_is_reported_without_failing_apply(self) -> None:
        """Post-apply EXIF readback is optional and must not invalidate a completed apply."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = self._make_plan(root, "img1.jpg", MediaKind.IMAGE)
            settings = AppSettings()
            settings.skip_backup = True
            settings.read_after_exif = True

            with patch("vmp.pipeline.apply._preflight_apply_tools"), patch(
                "vmp.pipeline.apply._produce_output",
                return_value=image.analysis.item.path,
            ), patch(
                "vmp.pipeline.apply.write_metadata"
            ), patch(
                "vmp.pipeline.apply.write_manifest"
            ) as write_manifest, patch(
                "vmp.pipeline.apply.read_metadata_batch",
                side_effect=RuntimeError("exiftool readback failed"),
            ) as read_metadata_batch, patch(
                "vmp.pipeline.apply.write_before_after_manifests"
            ) as write_before_after_manifests_mock:
                report = apply_plans(root, [image], settings)

        self.assertEqual(report.changed, 1)
        self.assertEqual(report.skipped, 0)
        self.assertEqual(len(report.errors), 0)
        self.assertTrue(any("Post-apply EXIF readback" in warning for warning in report.warnings))
        read_metadata_batch.assert_called_once()
        write_before_after_manifests_mock.assert_not_called()
        self.assertGreaterEqual(write_manifest.call_count, 2)

    def test_empty_work_tree_is_removed_after_apply(self) -> None:
        """A completed run should remove empty _VacationMediaProcessor_Work directories."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "20240624_120000"
            work_run = root / "_VacationMediaProcessor_Work" / run_id
            work_run.mkdir(parents=True)

            _cleanup_empty_generated_dirs(root, run_id)

            self.assertFalse(work_run.exists())
            self.assertFalse((root / "_VacationMediaProcessor_Work").exists())


class JpegMaintenanceTests(unittest.TestCase):
    """JPEG maintenance command regression tests."""

    def test_maintenance_uses_lossless_nconvert_flags(self) -> None:
        """Thumbnail/orientation maintenance must not add re-encode quality flags."""
        settings = AppSettings()
        settings.tools.xnconvert = "nconvert"
        with patch("vmp.tools.image.run_process") as run_process:
            maintain_jpeg(Path("in.jpg"), Path("out.jpg"), settings)
        args = run_process.call_args.args[0]
        self.assertIn("-jpegtrans", args)
        self.assertIn("exif", args)
        self.assertIn("-buildexifthumb", args)
        self.assertNotIn("-q", args)
        self.assertNotIn("-out", args)

    def test_maintenance_skips_backup_when_requested(self) -> None:
        """JPEG maintenance must honor the backup-skip toggle as well."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "photo.jpg"
            source.write_bytes(b"original")
            item = MediaItem(source, root, MediaKind.IMAGE)
            settings = AppSettings(skip_backup=True)

            def fake_maintain_jpeg(src: Path, target: Path, settings: AppSettings) -> None:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"fixed")

            with patch("vmp.pipeline.apply._resolve_required_tool"), patch(
                "vmp.pipeline.apply.discover_media",
                return_value=[item],
            ), patch(
                "vmp.pipeline.apply.maintain_jpeg",
                side_effect=fake_maintain_jpeg,
            ), patch(
                "vmp.pipeline.apply.write_manifest"
            ), patch(
                "vmp.pipeline.apply._backup_source"
            ) as backup_source:
                report = maintain_jpegs(root, settings)
            self.assertEqual(report.changed, 1)
            backup_source.assert_not_called()
            self.assertTrue(source.exists())

    def test_maintenance_creates_backup_by_default(self) -> None:
        """JPEG maintenance must still back up originals when the toggle is off."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "photo.jpg"
            source.write_bytes(b"original")
            item = MediaItem(source, root, MediaKind.IMAGE)
            settings = AppSettings(skip_backup=False)

            def fake_maintain_jpeg(src: Path, target: Path, settings: AppSettings) -> None:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"fixed")

            with patch("vmp.pipeline.apply._resolve_required_tool"), patch(
                "vmp.pipeline.apply.discover_media",
                return_value=[item],
            ), patch(
                "vmp.pipeline.apply.maintain_jpeg",
                side_effect=fake_maintain_jpeg,
            ), patch(
                "vmp.pipeline.apply.write_manifest"
            ), patch(
                "vmp.pipeline.apply._backup_source"
            ) as backup_source:
                report = maintain_jpegs(root, settings)
            self.assertEqual(report.changed, 1)
            backup_source.assert_called_once()
            self.assertTrue(source.exists())


class MetadataFilesystemDateTests(unittest.TestCase):
    """Filesystem timestamp write regression tests."""

    def _analysis(self, path: Path) -> AnalysisResult:
        item = MediaItem(path, path.parent, MediaKind.IMAGE)
        return AnalysisResult(
            item=item,
            metadata=RawMetadata(str(path), {}),
            resolved=ResolvedTimestamp(
                local_dt=datetime(2026, 1, 13, 15, 21, 0),
                utc_dt=None,
                offset=None,
                confidence=Confidence.HIGH,
            ),
            status=PlanStatus.OK,
        )

    def test_filesystem_date_tags_are_controlled_by_setting(self) -> None:
        """FileCreateDate/FileModifyDate tags should follow the filesystem-date toggle."""
        path = Path.cwd() / "photo.jpg"
        settings = AppSettings()
        enabled_tags, _ = metadata_write_tags(self._analysis(path), settings.metadata)
        self.assertEqual(enabled_tags["File:FileCreateDate"], "2026:01:13 15:21:00")
        self.assertEqual(enabled_tags["File:FileModifyDate"], "2026:01:13 15:21:00")

        settings.metadata.set_filesystem_dates = False
        disabled_tags, _ = metadata_write_tags(self._analysis(path), settings.metadata)
        self.assertNotIn("File:FileCreateDate", disabled_tags)
        self.assertNotIn("File:FileModifyDate", disabled_tags)

    def test_write_metadata_sets_real_filesystem_dates_from_local_time(self) -> None:
        """The real filesystem mtime/atime should be set to the local capture wall time."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "photo.jpg"
            path.write_bytes(b"data")
            analysis = self._analysis(path)
            settings = AppSettings()
            expected = datetime(2026, 1, 13, 15, 21, 0).timestamp()

            with patch("vmp.tools.run_process"), patch(
                "vmp.tools.os.utime"
            ) as utime, patch(
                "vmp.tools._set_windows_creation_time"
            ) as set_windows_creation_time:
                write_metadata(analysis, path, settings)

        utime.assert_called_once_with(path, (expected, expected))
        if os.name == "nt":
            set_windows_creation_time.assert_called_once_with(path, expected)

    def test_write_metadata_skips_real_filesystem_dates_when_disabled(self) -> None:
        """Disabling the toggle should leave real filesystem timestamps alone."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "photo.jpg"
            path.write_bytes(b"data")
            analysis = self._analysis(path)
            settings = AppSettings()
            settings.metadata.set_filesystem_dates = False

            with patch("vmp.tools.run_process"), patch(
                "vmp.tools.os.utime"
            ) as utime:
                write_metadata(analysis, path, settings)

        utime.assert_not_called()


class MetadataCleanupTests(unittest.TestCase):
    """Metadata cleanup regression tests."""

    def test_video_cleanup_does_not_emit_noisy_samsung_trailer_pseudo_tags(self) -> None:
        """Samsung trailer pseudo tags are not writable and should not be sent blindly."""
        root = Path.cwd()
        item = MediaItem(root / "clip.mov", root, MediaKind.VIDEO)
        analysis = AnalysisResult(
            item=item,
            metadata=RawMetadata(str(item.path), {"QuickTime:CreateDate": "2026:05:03 18:47:51"}),
            resolved=ResolvedTimestamp(
                local_dt=datetime(2026, 5, 3, 19, 46, 21),
                utc_dt=datetime(2026, 5, 3, 18, 47, 51),
                offset=timedelta(hours=2),
                confidence=Confidence.HIGH,
            ),
            status=PlanStatus.OK,
        )

        _set_tags, delete_tags = metadata_write_tags(analysis, AppSettings().metadata)

        self.assertIn("Trailer:All", delete_tags)
        self.assertIn("trailer:all", delete_tags)
        self.assertNotIn("Samsung:Trailer", delete_tags)
        self.assertNotIn("Samsung:TrailerOffset", delete_tags)
        self.assertNotIn("Samsung:TrailerLength", delete_tags)


class ImageConversionOrientationTests(unittest.TestCase):
    """Regression tests for the double-rotation fix during image conversion."""

    def test_jpeg_conversion_bakes_exif_orientation_losslessly_first(self) -> None:
        """JPEG sources must get a lossless -jpegtrans exif pre-pass before re-encode."""
        settings = AppSettings()
        settings.tools.xnconvert = "nconvert"
        with patch("vmp.tools.image.run_process") as run_process:
            convert_image(Path("in.jpg"), Path("out.jpg"), settings)
        invocations = [call.args[0] for call in run_process.call_args_list]
        bake = invocations[0]
        self.assertIn("-jpegtrans", bake)
        self.assertIn("exif", bake)
        self.assertNotIn("-q", bake)
        self.assertNotIn("-out", bake)
        encode = invocations[1]
        self.assertIn("-out", encode)
        self.assertIn("jpeg", encode)
        self.assertIn("-q", encode)
        self.assertNotIn("-jpegtrans", encode)

    def test_heic_conversion_uses_dedicated_heic_quality(self) -> None:
        """HEIC/HEIF conversions must use the dedicated HEIC/JPEG quality setting."""
        settings = AppSettings()
        settings.tools.xnconvert = "nconvert"
        settings.images.jpeg_quality = 74
        settings.images.heic_heif_jpeg_quality = 92
        with patch("vmp.tools.image.run_process") as run_process:
            convert_image(Path("photo.heic"), Path("out.jpg"), settings)
        invocations = [call.args[0] for call in run_process.call_args_list]
        self.assertEqual(len(invocations), 1)
        encode = invocations[0]
        self.assertIn("-q", encode)
        self.assertIn("92", encode)
        self.assertNotIn("74", encode)

    def test_heic_conversion_does_not_add_lossless_jpegtrans_prepass(self) -> None:
        """HEIC/HEIF sources must not get -jpegtrans exif; libheif already bakes orientation."""
        settings = AppSettings()
        settings.tools.xnconvert = "nconvert"
        with patch("vmp.tools.image.run_process") as run_process:
            convert_image(Path("photo.heic"), Path("out.jpg"), settings)
        invocations = [call.args[0] for call in run_process.call_args_list]
        self.assertEqual(len(invocations), 1)
        encode = invocations[0]
        self.assertIn("-out", encode)
        self.assertIn("jpeg", encode)
        self.assertNotIn("-jpegtrans", encode)

    def test_png_conversion_does_not_add_lossless_jpegtrans_prepass(self) -> None:
        """PNG sources carry no EXIF orientation and must not get -jpegtrans exif."""
        settings = AppSettings()
        settings.tools.xnconvert = "nconvert"
        with patch("vmp.tools.image.run_process") as run_process:
            convert_image(Path("icon.png"), Path("out.jpg"), settings)
        invocations = [call.args[0] for call in run_process.call_args_list]
        self.assertEqual(len(invocations), 1)
        encode = invocations[0]
        self.assertNotIn("-jpegtrans", encode)

    def test_copy_all_metadata_strips_orientation_tag_for_images(self) -> None:
        """Image metadata copy must delete Orientation, not force it to 1."""
        settings = AppSettings()
        settings.tools.exiftool = "exiftool"
        with patch("vmp.tools.run_process") as run_process:
            copy_all_metadata(Path("src.jpg"), Path("dst.jpg"), settings, MediaKind.IMAGE)
        args = run_process.call_args.args[0]
        self.assertIn("-IFD0:Orientation=", args)
        self.assertIn("-XMP-tiff:Orientation=", args)
        self.assertNotIn("-IFD0:Orientation=1", args)

    def test_copy_all_metadata_leaves_orientation_for_videos(self) -> None:
        """Video metadata copy must not touch Orientation (not an image concept)."""
        settings = AppSettings()
        settings.tools.exiftool = "exiftool"
        with patch("vmp.tools.run_process") as run_process:
            copy_all_metadata(Path("src.mov"), Path("dst.mp4"), settings, MediaKind.VIDEO)
        args = run_process.call_args.args[0]
        self.assertNotIn("-IFD0:Orientation=", args)
        self.assertNotIn("-XMP-tiff:Orientation=", args)

    def test_video_transcode_can_apply_full_hd_scale_filter(self) -> None:
        """FFmpeg transcode should receive a scale filter when Full HD limiting is enabled."""
        from vmp.tools import transcode_video

        settings = AppSettings()
        settings.tools.ffmpeg = "ffmpeg"
        with patch("vmp.tools.video.run_process") as run_process:
            transcode_video(Path("src.mp4"), Path("dst.mp4"), 25, settings, (1920, 1080))
        args = run_process.call_args.args[0]
        self.assertIn("-vf", args)
        vf_index = args.index("-vf")
        self.assertIn("scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease", args[vf_index + 1])
        self.assertIn("format=yuv420p", args[vf_index + 1])

    def test_video_transcode_combines_scale_and_30_fps_in_one_filter_graph(self) -> None:
        """Resolution, FPS, pixel format, and codec should share one FFmpeg command."""
        from vmp.tools import transcode_video

        settings = AppSettings()
        settings.tools.ffmpeg = "ffmpeg"
        with patch("vmp.tools.video.run_process") as run_process:
            transcode_video(
                Path("src.mp4"),
                Path("dst.mp4"),
                25,
                settings,
                (1920, 1080),
                max_fps=30,
            )
        args = run_process.call_args.args[0]
        self.assertEqual(args.count("-vf"), 1)
        filter_graph = args[args.index("-vf") + 1]
        self.assertEqual(
            filter_graph,
            "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease,fps=30,format=yuv420p",
        )
        self.assertIn("-c:v", args)
        self.assertEqual(args[args.index("-c:v") + 1], settings.videos.encoder)

    def test_video_transcode_drops_alpha_for_x265_even_without_scaling(self) -> None:
        """x265 cannot encode yuva formats, so transcodes should force a non-alpha pixel format."""
        from vmp.tools import transcode_video

        settings = AppSettings()
        settings.tools.ffmpeg = "ffmpeg"
        with patch("vmp.tools.video.run_process") as run_process:
            transcode_video(Path("src.mov"), Path("dst.mp4"), 25, settings)
        args = run_process.call_args.args[0]
        self.assertIn("-vf", args)
        vf_index = args.index("-vf")
        self.assertEqual(args[vf_index + 1], "format=yuv420p")


class IdempotencyTests(unittest.TestCase):
    """Regression tests for DONE detection and idempotent planning."""

    def test_file_with_vmp_comment_is_marked_done(self) -> None:
        """A file with an existing VMP normalizer comment must be PlanStatus.DONE."""
        root = Path.cwd()
        item = MediaItem(root / "IMG_20240101_120000.jpg", root, MediaKind.IMAGE)
        raw = RawMetadata(
            source_file=str(item.path),
            tags={
                "EXIF:DateTimeOriginal": "2024:01:01 12:00:00",
                "EXIF:UserComment": "VacationMediaProcessor normalized timestamps; local=2024-01-01 12:00:00; utc=-; offset=-; confidence=high",
            },
        )
        settings = AppSettings()
        result = analyze_item(item, raw, settings.metadata)
        self.assertEqual(result.status, PlanStatus.DONE)

    def test_file_without_vmp_comment_is_not_done(self) -> None:
        """A file without VMP comment must not be PlanStatus.DONE."""
        root = Path.cwd()
        item = MediaItem(root / "IMG_20240101_120000.jpg", root, MediaKind.IMAGE)
        raw = RawMetadata(
            source_file=str(item.path),
            tags={"EXIF:DateTimeOriginal": "2024:01:01 12:00:00"},
        )
        settings = AppSettings()
        result = analyze_item(item, raw, settings.metadata)
        self.assertNotEqual(result.status, PlanStatus.DONE)

    def test_done_image_gets_no_convert_or_transcode(self) -> None:
        """A DONE image must not get IMAGE_CONVERT or VIDEO_TRANSCODE actions."""
        root = Path.cwd()
        item = MediaItem(root / "IMG_20240101_120000.jpg", root, MediaKind.IMAGE)
        raw = RawMetadata(
            source_file=str(item.path),
            tags={
                "EXIF:DateTimeOriginal": "2024:01:01 12:00:00",
                "EXIF:UserComment": "VacationMediaProcessor normalized timestamps; local=2024-01-01 12:00:00; utc=-; offset=-; confidence=high",
            },
        )
        settings = AppSettings()
        result = analyze_item(item, raw, settings.metadata)
        plan = build_plans([result], settings)[0]
        action_kinds = {action.kind.value for action in plan.actions}
        self.assertNotIn("image_convert", action_kinds)
        self.assertNotIn("video_transcode", action_kinds)
        self.assertIn("metadata_normalize", action_kinds)

    def test_done_video_gets_no_transcode(self) -> None:
        """A DONE video must not get VIDEO_TRANSCODE."""
        root = Path.cwd()
        item = MediaItem(root / "VID_20240101_120000.mp4", root, MediaKind.VIDEO)
        raw = RawMetadata(
            source_file=str(item.path),
            tags={
                "Keys:CreationDate": "2024-01-01T12:00:00+01:00",
                "QuickTime:Comment": "VacationMediaProcessor normalized timestamps; local=2024-01-01 12:00:00; utc=2024-01-01 11:00:00; offset=+01:00; confidence=high",
            },
        )
        settings = AppSettings()
        result = analyze_item(item, raw, settings.metadata)
        plan = build_plans([result], settings)[0]
        action_kinds = {action.kind.value for action in plan.actions}
        self.assertNotIn("video_transcode", action_kinds)
        self.assertIn("metadata_normalize", action_kinds)

    def test_done_file_final_path_is_original(self) -> None:
        """A DONE file must keep its original path (no rename)."""
        root = Path.cwd()
        item = MediaItem(root / "IMG_20240101_120000.jpg", root, MediaKind.IMAGE)
        raw = RawMetadata(
            source_file=str(item.path),
            tags={
                "EXIF:DateTimeOriginal": "2024:01:01 12:00:00",
                "EXIF:UserComment": "VacationMediaProcessor normalized timestamps; local=2024-01-01 12:00:00; utc=-; offset=-; confidence=high",
            },
        )
        settings = AppSettings()
        result = analyze_item(item, raw, settings.metadata)
        plan = build_plans([result], settings)[0]
        self.assertEqual(plan.final_path, item.path)


class ColumnWidthDistributionTests(unittest.TestCase):
    """Priority-based fit-to-width for the media table columns."""

    DESIRED = {"a": 100, "b": 100, "action": 200}
    MINS = {"a": 40, "b": 40, "action": 60}
    PRIO = {"a": 0, "b": 1, "action": 5}

    def test_surplus_goes_to_absorber(self) -> None:
        widths = distribute_column_widths(self.DESIRED, self.MINS, self.PRIO, 500, "action")
        self.assertEqual(sum(widths.values()), 500)
        self.assertEqual(widths["a"], 100)
        self.assertEqual(widths["b"], 100)
        self.assertEqual(widths["action"], 300)  # absorbed the extra 100

    def test_exact_fit_unchanged(self) -> None:
        widths = distribute_column_widths(self.DESIRED, self.MINS, self.PRIO, 400, "action")
        self.assertEqual(widths, dict(self.DESIRED))

    def test_shrinks_highest_priority_first(self) -> None:
        # available 360 -> deficit 40; action (prio 5) shrinks before a/b.
        widths = distribute_column_widths(self.DESIRED, self.MINS, self.PRIO, 360, "action")
        self.assertEqual(sum(widths.values()), 360)
        self.assertEqual(widths["action"], 160)
        self.assertEqual(widths["a"], 100)
        self.assertEqual(widths["b"], 100)

    def test_cascades_to_next_priority_when_absorber_at_min(self) -> None:
        # deficit 180: action gives 140 (down to its min 60), then b (prio 1) gives 40.
        widths = distribute_column_widths(self.DESIRED, self.MINS, self.PRIO, 220, "action")
        self.assertEqual(sum(widths.values()), 220)
        self.assertEqual(widths["action"], 60)
        self.assertEqual(widths["b"], 60)
        self.assertEqual(widths["a"], 100)

    def test_never_below_minimums(self) -> None:
        # Tiny width: all columns clamp to their minimums (sum may exceed available).
        widths = distribute_column_widths(self.DESIRED, self.MINS, self.PRIO, 50, "action")
        self.assertEqual(widths["a"], 40)
        self.assertEqual(widths["b"], 40)
        self.assertEqual(widths["action"], 60)


class PngConversionToggleTests(unittest.TestCase):
    """PNG conversion must keep extension and conversion action consistent."""

    def _png_result(self) -> AnalysisResult:
        root = Path.cwd()
        item = MediaItem(root / "graphic.png", root, MediaKind.IMAGE)
        settings = AppSettings()
        result = analyze_item(
            item,
            RawMetadata(str(item.path), {"EXIF:DateTimeOriginal": "2024:01:01 12:00:00"}),
            settings.metadata,
        )
        result.status = PlanStatus.OK
        return result

    def test_png_kept_as_png_and_not_converted_when_toggle_off(self) -> None:
        """With the PNG toggle off, a PNG must not be renamed to .jpg without conversion."""
        settings = AppSettings()
        settings.images.png_to_jpg = False
        plan = build_plans([self._png_result()], settings)[0]
        self.assertIsNotNone(plan.final_path)
        self.assertEqual(plan.final_path.suffix, ".png")
        self.assertNotIn(ActionKind.IMAGE_CONVERT, {action.kind for action in plan.actions})

    def test_png_converted_to_jpg_when_toggle_on(self) -> None:
        """With the PNG toggle on, a PNG becomes .jpg and gets a conversion action."""
        settings = AppSettings()
        settings.images.png_to_jpg = True
        plan = build_plans([self._png_result()], settings)[0]
        self.assertIsNotNone(plan.final_path)
        self.assertEqual(plan.final_path.suffix, ".jpg")
        self.assertIn(ActionKind.IMAGE_CONVERT, {action.kind for action in plan.actions})


class TimestampInferencePriorityTests(unittest.TestCase):
    """Real capture-time tags must win over the cross-value offset heuristic."""

    def test_real_datetime_original_wins_over_later_modify_date(self) -> None:
        """DateTimeOriginal must not be overridden by a ModifyDate an hour later."""
        root = Path.cwd()
        item = MediaItem(root / "IMG_0001.jpg", root, MediaKind.IMAGE)
        raw = RawMetadata(
            source_file=str(item.path),
            tags={
                "EXIF:DateTimeOriginal": "2024:07:01 12:00:00",
                "EXIF:CreateDate": "2024:07:01 12:00:00",
                "IFD0:ModifyDate": "2024:07:01 13:00:00",
            },
        )
        resolved = resolve_timestamp(item, raw, tolerance_seconds=180)
        self.assertEqual(resolved.local_dt, datetime(2024, 7, 1, 12, 0, 0))
        self.assertIsNone(resolved.offset)

    def test_conflicting_local_values_warn_even_when_offset_inferable(self) -> None:
        """A capture/modify conflict must surface as a WARN, not be silently swallowed."""
        root = Path.cwd()
        item = MediaItem(root / "IMG_0002.jpg", root, MediaKind.IMAGE)
        raw = RawMetadata(
            source_file=str(item.path),
            tags={
                "EXIF:DateTimeOriginal": "2024:07:01 12:00:00",
                "IFD0:ModifyDate": "2024:07:01 13:00:00",
            },
        )
        result = analyze_item(item, raw, AppSettings().metadata)
        self.assertEqual(result.status, PlanStatus.WARN)
        self.assertTrue(any("Conflicting local metadata" in w for w in result.warnings))


class OffsetBoundTests(unittest.TestCase):
    """Derived timezone offsets must stay within ±14:00."""

    def test_derive_offset_rejects_implausibly_large_offset(self) -> None:
        from vmp.metadata import maybe_derive_offset

        local_dt = datetime(2024, 7, 1, 23, 0, 0)
        utc_dt = datetime(2024, 7, 1, 4, 0, 0)  # 19h apart, a multiple of 15 min
        self.assertIsNone(maybe_derive_offset(local_dt, utc_dt, tolerance_seconds=180))

    def test_derive_offset_accepts_normal_offset(self) -> None:
        from vmp.metadata import maybe_derive_offset

        local_dt = datetime(2024, 7, 1, 13, 0, 0)
        utc_dt = datetime(2024, 7, 1, 12, 0, 0)  # +01:00
        self.assertEqual(maybe_derive_offset(local_dt, utc_dt, tolerance_seconds=180), timedelta(hours=1))


class MetadataBatchRobustnessTests(unittest.TestCase):
    """A failed ExifTool batch must degrade to SKIP, not crash the scan."""

    def _run(self, returncode: int, stdout: str) -> dict:
        from vmp.metadata import read_metadata_batch
        from vmp.core.processes import ProcessResult

        root = Path.cwd()
        items = [MediaItem(root / f"img{i}.jpg", root, MediaKind.IMAGE) for i in range(3)]
        fake = ProcessResult(args=[], returncode=returncode, stdout=stdout, stderr="boom")
        with patch("vmp.metadata.run_process", return_value=fake):
            return read_metadata_batch(items, "exiftool")

    def test_empty_output_returns_no_records_instead_of_raising(self) -> None:
        self.assertEqual(self._run(1, ""), {})

    def test_unparseable_output_returns_no_records(self) -> None:
        self.assertEqual(self._run(1, "not json at all"), {})

    def test_partial_output_keeps_good_records(self) -> None:
        root = Path.cwd()
        good = str(root / "img1.jpg")
        payload = json.dumps([{"SourceFile": good, "EXIF:DateTimeOriginal": "2024:01:01 12:00:00"}])
        records = self._run(1, payload)
        self.assertIn(Path(good).resolve(), records)


class AudioCopySafetyTests(unittest.TestCase):
    """Stream copy must only keep MP4-safe audio; PCM and friends re-encode to AAC."""

    def _args_for_codec(self, source_codec: str) -> list[str]:
        from vmp.tools import _audio_transcode_args

        settings = AppSettings()
        settings.videos.audio_codec = "copy"
        settings.videos.audio_bitrate = "160k"
        payload = {"streams": [{"codec_type": "audio", "codec_name": source_codec}]}
        with patch("vmp.tools.video.probe_video", return_value=payload):
            return _audio_transcode_args(Path("src.mov"), settings)

    def test_pcm_is_reencoded_to_aac(self) -> None:
        self.assertEqual(self._args_for_codec("pcm_s16le"), ["-c:a", "aac", "-b:a", "160k"])

    def test_aac_is_copied(self) -> None:
        self.assertEqual(self._args_for_codec("aac"), ["-c:a", "copy"])


class HeicOrientationGuardTests(unittest.TestCase):
    """The HEIC output orientation guard must catch only clear transpose mismatches."""

    def test_flags_transposed_output(self) -> None:
        from vmp.tools import _output_needs_orientation_fix

        # Correct display is portrait 3024x4032; nconvert left it landscape 4032x3024.
        self.assertTrue(_output_needs_orientation_fix((3024, 4032), (4032, 3024)))

    def test_does_not_flag_correct_output(self) -> None:
        from vmp.tools import _output_needs_orientation_fix

        self.assertFalse(_output_needs_orientation_fix((3024, 4032), (3024, 4032)))

    def test_does_not_flag_square_or_missing(self) -> None:
        from vmp.tools import _output_needs_orientation_fix

        self.assertFalse(_output_needs_orientation_fix((1000, 1000), (1000, 1000)))
        self.assertFalse(_output_needs_orientation_fix(None, (4032, 3024)))
        self.assertFalse(_output_needs_orientation_fix((3024, 4032), None))


class ReentrancyGuardTests(unittest.TestCase):
    """A new run must be refused while a worker thread is still running."""

    def _window(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        with patch("vmp.gui.main.window.load_settings", return_value=AppSettings()), patch(
            "vmp.gui.main.window.configure_logging", return_value=Path("log.txt")
        ), patch("vmp.gui.main.window.setup_gui_logging"), patch(
            "vmp.gui.main.window.resolve_executable", return_value="tool"
        ):
            return MainWindow()

    def test_scan_is_blocked_while_work_is_running(self) -> None:
        window = self._window()
        with patch.object(MainWindow, "_has_running_work", return_value=True), patch(
            "vmp.gui.main.worker_lifecycle.QThread"
        ) as thread_cls, patch.object(QMessageBox, "information") as info:
            window.scan(Path.cwd())
        thread_cls.assert_not_called()
        info.assert_called_once()

    def test_abort_waits_for_safe_unwind_without_terminating_qthread(self) -> None:
        """Closing during Apply must never hard-kill a thread inside file I/O."""
        window = self._window()
        thread = MagicMock()
        thread.isRunning.side_effect = [True, False]
        thread.wait.return_value = False
        window.worker_thread = thread

        with patch("vmp.gui.main.worker_lifecycle.kill_active_processes", return_value=2):
            window._abort_running_work()

        thread.requestInterruption.assert_called_once()
        thread.quit.assert_called_once()
        thread.wait.assert_called_once_with(1000)
        thread.terminate.assert_not_called()


class FolderDropTests(unittest.TestCase):
    """Folder drops must honor batch behavior and the single-worker scan queue."""

    def _window(self) -> MainWindow:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        self._app = app
        with patch("vmp.gui.main.window.load_settings", return_value=AppSettings()), patch(
            "vmp.gui.main.window.configure_logging", return_value=Path("log.txt")
        ), patch("vmp.gui.main.window.setup_gui_logging"), patch(
            "vmp.gui.main.window.resolve_executable", return_value="tool"
        ):
            return MainWindow()

    def test_multiple_folders_on_empty_list_build_one_plan_and_start_one_scan(self) -> None:
        window = self._window()
        with tempfile.TemporaryDirectory() as tmp:
            folders = [Path(tmp) / name for name in ("one", "two", "three")]
            for folder in folders:
                folder.mkdir()
                (folder / f"{folder.name}.jpg").write_bytes(b"image")
            with patch.object(window, "scan") as scan:
                window._handle_dropped_folders(folders)

        self.assertEqual(window.roots, [folder.resolve() for folder in folders])
        self.assertEqual(len(window.plans), 3)
        scan.assert_called_once()
        self.assertEqual(len(scan.call_args.kwargs["items"]), 3)

    def test_add_setting_starts_the_complete_dropped_batch_additively(self) -> None:
        window = self._window()
        window.settings_model.folder_drop_behavior = "add"
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "existing"
            first = Path(tmp) / "first"
            second = Path(tmp) / "second"
            for folder in (existing, first, second):
                folder.mkdir()
            window.roots = [existing.resolve()]
            with patch.object(window, "_start_dropped_folder_batch") as start_batch:
                window._handle_dropped_folders([first, second])

        start_batch.assert_called_once_with([first.resolve(), second.resolve()], replace_existing=False)

    def test_replace_setting_discards_older_queue_and_replaces_with_complete_batch(self) -> None:
        window = self._window()
        window.settings_model.folder_drop_behavior = "replace"
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "existing"
            stale = Path(tmp) / "stale"
            first = Path(tmp) / "first"
            second = Path(tmp) / "second"
            for folder in (existing, stale, first, second):
                folder.mkdir()
            window.roots = [existing.resolve()]
            window._pending_folders = [(stale.resolve(), False)]
            with patch.object(window, "_discover_and_scan_roots") as discover_and_scan:
                window._start_dropped_folder_batch([first.resolve(), second.resolve()], replace_existing=True)

        self.assertEqual(window._pending_folders, [])
        self.assertEqual(window.roots, [first.resolve(), second.resolve()])
        discover_and_scan.assert_called_once_with([first.resolve(), second.resolve()])

    def test_default_ask_setting_applies_answer_to_the_whole_batch(self) -> None:
        window = self._window()
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "existing"
            folders = [Path(tmp) / name for name in ("one", "two")]
            for folder in (existing, *folders):
                folder.mkdir()
            window.roots = [existing.resolve()]
            with patch.object(
                QMessageBox, "question", return_value=QMessageBox.StandardButton.No
            ) as question, patch.object(window, "_start_dropped_folder_batch") as start_batch:
                window._handle_dropped_folders(folders)

        question.assert_called_once()
        start_batch.assert_called_once_with(
            [folders[0].resolve(), folders[1].resolve()], replace_existing=False
        )

    def test_drop_during_scan_is_queued_without_starting_a_second_worker(self) -> None:
        window = self._window()
        window.settings_model.folder_drop_behavior = "add"
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "existing"
            dropped = Path(tmp) / "dropped"
            existing.mkdir()
            dropped.mkdir()
            window.roots = [existing.resolve()]
            with patch.object(window, "_scan_is_running", return_value=True), patch.object(
                window, "_has_running_work", return_value=True
            ), patch.object(window, "_process_pending_folders") as process:
                window._handle_dropped_folders([dropped])

        self.assertEqual(window._pending_folders, [(dropped.resolve(), False)])
        process.assert_not_called()
        self.assertIn("1", window.status_label.text())

    def test_controls_stay_busy_between_queued_folder_scans(self) -> None:
        window = self._window()
        window._pending_folders = [(Path.cwd(), False)]
        window._set_busy(True)

        window.on_scan_finished([], [])

        self.assertFalse(window.scan_button.isEnabled())
        self.assertIn("1", window.status_label.text())


class VideoNotSmallerTests(unittest.TestCase):
    """A transcode that is not smaller must be a deliberate skip, not an error."""

    def test_not_smaller_records_warning_and_skip_not_error(self) -> None:
        from unittest.mock import patch

        from vmp.core.models import PipelineReport
        from vmp.pipeline import (
            VideoNotSmallerError,
            _apply_one_plan,
            _record_outcome,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video_path = root / "clip.mp4"
            video_path.write_bytes(b"b" * 200)
            plan = MediaPlan(
                analysis=AnalysisResult(
                    item=MediaItem(video_path, root, MediaKind.VIDEO),
                    metadata=RawMetadata(str(video_path), {}),
                    resolved=ResolvedTimestamp(None, None, None, Confidence.LOW),
                    status=PlanStatus.OK,
                ),
                final_path=video_path,
            )
            settings = AppSettings()
            settings.skip_backup = True
            with patch(
                "vmp.pipeline.apply._produce_output",
                side_effect=VideoNotSmallerError("Encoded video is larger than the source; original kept."),
            ):
                outcome = _apply_one_plan(plan, settings, "runid", None, 1, 1)

        self.assertTrue(outcome.skipped)
        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.errors, [])
        self.assertEqual(len(outcome.warnings), 1)
        self.assertIn("original kept", outcome.warnings[0])

        report = PipelineReport(run_id="runid")
        _record_outcome(report, outcome)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(report.errors, [])
        self.assertEqual(len(report.warnings), 1)


class LoggingTests(unittest.TestCase):
    """PID-specific log files and stale-log pruning."""

    def test_log_filename_is_pid_specific(self) -> None:
        import os

        from vmp.core.logging_config import _log_filename

        self.assertEqual(_log_filename(), f"vmp.{os.getpid()}.log")

    def test_run_ids_include_microseconds_for_same_second_runs(self) -> None:
        """Fast or concurrent runs must not share work, backup, or manifest paths."""
        from vmp.pipeline import make_run_id

        with patch("vmp.pipeline.shared.datetime") as mocked_datetime:
            mocked_datetime.now.side_effect = [
                datetime(2026, 7, 16, 12, 0, 0, 1),
                datetime(2026, 7, 16, 12, 0, 0, 2),
            ]
            first = make_run_id()
            second = make_run_id()

        self.assertEqual(first, "20260716_120000_000001")
        self.assertEqual(second, "20260716_120000_000002")
        self.assertLess(first, second)

    def test_backup_variant_only_accepts_numeric_collision_suffix(self) -> None:
        """A similarly named original must not be mistaken for another file's backup."""
        from vmp.gui.common.backup_discovery import existing_backup_variant

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requested = root / "photo.jpg"
            unrelated = root / "photo-edited.jpg"
            numeric = root / "photo-2.jpg"
            unrelated.write_bytes(b"wrong")

            self.assertIsNone(existing_backup_variant(requested))

            numeric.write_bytes(b"right")
            self.assertEqual(existing_backup_variant(requested), numeric)

    def test_prune_removes_only_stale_foreign_logs(self) -> None:
        import os
        import time

        from vmp.core.logging_config import _prune_stale_logs

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            mine = directory / f"vmp.{os.getpid()}.log"
            mine.write_text("mine", encoding="utf-8")
            old = directory / "vmp.999999.log"
            old_rot = directory / "vmp.999999.log.1"
            fresh = directory / "vmp.888888.log"
            for path in (old, old_rot, fresh):
                path.write_text("x", encoding="utf-8")
            stale = time.time() - 10 * 86400
            os.utime(old, (stale, stale))
            os.utime(old_rot, (stale, stale))

            _prune_stale_logs(directory)

            self.assertTrue(mine.exists())
            self.assertTrue(fresh.exists())
            self.assertFalse(old.exists())
            self.assertFalse(old_rot.exists())

    def test_resilient_handler_survives_locked_rotation(self) -> None:
        import logging
        from unittest.mock import patch

        from vmp.core.logging_config import _ResilientRotatingFileHandler

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            handler = _ResilientRotatingFileHandler(str(path), maxBytes=50, backupCount=3, encoding="utf-8")
            logger = logging.getLogger("vmp_test_resilient")
            logger.handlers = [handler]
            logger.setLevel(logging.INFO)
            with patch("os.rename", side_effect=PermissionError(32, "in use")):
                for index in range(20):
                    logger.info("padding padding padding line %d", index)
            handler.close()
            self.assertGreater(path.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
