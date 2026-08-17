"""Load and save application settings."""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from .models import (
    ApplyMode,
    AppSettings,
    DiffToolSettings,
    GpsRepairSettings,
    ImageSettings,
    MapSettings,
    MetadataSettings,
    ToolPaths,
    VideoSettings,
)

T = TypeVar("T")


def settings_dir() -> Path:
    """Return the per-user settings directory."""
    base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    return base / "VacationMediaProcessor"


def settings_path() -> Path:
    """Return the JSON settings file path."""
    return settings_dir() / "settings.json"


def fallback_settings_path() -> Path:
    """Return a local fallback settings path for restricted environments."""
    return Path.cwd() / ".vmp_settings" / "settings.json"


def _decode_datetime(value: Any) -> datetime | None:
    """Decode an ISO datetime or return None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _dataclass_from_dict(cls: type[T], payload: dict[str, Any]) -> T:
    """Construct a dataclass from a partial dictionary."""
    kwargs: dict[str, Any] = {}
    for item in fields(cls):  # type: ignore[arg-type]
        if item.name not in payload:
            continue
        value = payload[item.name]
        if item.name in {"range_start", "range_end"}:
            value = _decode_datetime(value)
        if item.name == "apply_mode":
            try:
                value = ApplyMode(value)
            except ValueError:
                value = ApplyMode.FULL_NORMALIZE
        kwargs[item.name] = value
    return cls(**kwargs)  # type: ignore[misc]


def _safe_int(value: Any, default: int) -> int:
    """Return an integer setting or a default."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float) -> float:
    """Return a finite float setting or a default."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _optional_int(value: Any) -> int | None:
    """Return an optional integer setting."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _cache_mode(value: Any) -> str:
    """Return a supported Lasso thumbnail cache mode."""
    text = str(value or "ram").lower()
    return text if text in {"ram", "disk", "off"} else "ram"


def _folder_drop_behavior(value: Any) -> str:
    """Return the action used when folders are dropped onto an existing list."""
    text = str(value or "ask").lower()
    return text if text in {"ask", "add", "replace"} else "ask"


def _language(value: Any) -> str:
    """Return a supported UI language preference ('auto' or an available code)."""
    from .i18n import available_languages

    text = str(value or "auto").lower()
    return text if text == "auto" or text in available_languages() else "auto"


def _clamped_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """Return an integer setting clamped into a safe range."""
    return max(minimum, min(maximum, _safe_int(value, default)))


def _clamped_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    """Return a floating-point setting clamped into a safe range."""
    return max(minimum, min(maximum, _safe_float(value, default)))


def _read_settings_payload() -> dict[str, Any]:
    """Read the settings JSON, falling back to the local copy on any failure.

    A truncated/corrupt primary file must not silently reset the user's settings
    while an intact fallback copy exists — try both locations before defaulting.
    """
    for path in (settings_path(), fallback_settings_path()):
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def load_settings() -> AppSettings:
    """Load settings from disk, returning defaults when no file exists."""
    payload = _read_settings_payload()
    if not payload:
        return AppSettings()
    try:
        return _settings_from_payload(payload)
    except Exception:  # noqa: BLE001 - a hand-edited file must never block startup
        return AppSettings()


def _settings_from_payload(payload: dict[str, Any]) -> AppSettings:
    """Build AppSettings from a parsed JSON payload."""
    tools = _dataclass_from_dict(ToolPaths, payload.get("tools", {}))
    if tools.xnconvert.lower() == r"c:\program files\xnconvert\xnconvert.exe":
        tools.xnconvert = "nconvert"
    diff_tools = _dataclass_from_dict(DiffToolSettings, payload.get("diff_tools", {}))
    images = _dataclass_from_dict(ImageSettings, payload.get("images", {}))
    images.parallel_workers = _clamped_int(images.parallel_workers, 8, 1, 16)
    videos = _dataclass_from_dict(VideoSettings, payload.get("videos", {}))
    metadata = _dataclass_from_dict(MetadataSettings, payload.get("metadata", {}))
    gps_payload = payload.get("gps_repair", {})
    gps_repair = _dataclass_from_dict(GpsRepairSettings, gps_payload if isinstance(gps_payload, dict) else {})
    gps_repair.single_safe_minutes = _clamped_int(gps_repair.single_safe_minutes, 2, 1, 120)
    gps_repair.pair_safe_minutes = _clamped_int(gps_repair.pair_safe_minutes, 30, 1, 1440)
    gps_repair.pair_safe_distance_km = _clamped_float(gps_repair.pair_safe_distance_km, 5.0, 0.1, 500.0)
    gps_repair.pair_safe_speed_kmh = _clamped_float(gps_repair.pair_safe_speed_kmh, 20.0, 1.0, 300.0)
    gps_repair.cluster_min_anchors = _clamped_int(gps_repair.cluster_min_anchors, 3, 3, 20)
    gps_repair.cluster_min_span_minutes = _clamped_int(gps_repair.cluster_min_span_minutes, 5, 1, 1440)
    gps_repair.cluster_radius_m = _clamped_int(gps_repair.cluster_radius_m, 200, 10, 5000)
    gps_repair.cluster_max_gap_minutes = _clamped_int(gps_repair.cluster_max_gap_minutes, 60, 1, 1440)
    gps_repair.review_pair_max_hours = _clamped_int(gps_repair.review_pair_max_hours, 4, 1, 48)
    gps_repair.review_pair_max_distance_km = _clamped_float(
        gps_repair.review_pair_max_distance_km, 50.0, 1.0, 2000.0
    )
    gps_repair.review_single_max_minutes = _clamped_int(gps_repair.review_single_max_minutes, 30, 1, 1440)
    gps_repair.review_single_max_minutes = max(
        gps_repair.review_single_max_minutes, gps_repair.single_safe_minutes
    )
    gps_repair.review_pair_max_distance_km = max(
        gps_repair.review_pair_max_distance_km, gps_repair.pair_safe_distance_km
    )
    gps_repair.review_pair_max_hours = max(
        gps_repair.review_pair_max_hours, (gps_repair.pair_safe_minutes + 59) // 60
    )
    maps_payload = payload.get("maps", {})
    maps = _dataclass_from_dict(MapSettings, maps_payload if isinstance(maps_payload, dict) else {})
    from ..map_providers import normalize_provider_id

    maps.provider = normalize_provider_id(maps.provider)
    maps.mapy_api_key = str(maps.mapy_api_key or "").strip()
    return AppSettings(
        recursive=bool(payload.get("recursive", True)),
        language=_language(payload.get("language", "auto")),
        folder_drop_behavior=_folder_drop_behavior(payload.get("folder_drop_behavior", "ask")),
        skip_backup=bool(payload.get("skip_backup", False)),
        read_after_exif=bool(payload.get("read_after_exif", False)),
        table_font_size=_safe_int(payload.get("table_font_size"), 10),
        window_x=_optional_int(payload.get("window_x")),
        window_y=_optional_int(payload.get("window_y")),
        window_width=_safe_int(payload.get("window_width"), 1080),
        window_height=_safe_int(payload.get("window_height"), 680),
        main_window_geometry=str(payload.get("main_window_geometry", "") or ""),
        lasso_window_geometry=str(payload.get("lasso_window_geometry", "") or ""),
        pair_window_geometry=str(payload.get("pair_window_geometry", "") or ""),
        pair_viewer_geometry=str(payload.get("pair_viewer_geometry", "") or ""),
        gps_repair_window_geometry=str(payload.get("gps_repair_window_geometry", "") or ""),
        lasso_load_target_after_move=bool(payload.get("lasso_load_target_after_move", False)),
        lasso_thumbnail_cache_mode=_cache_mode(payload.get("lasso_thumbnail_cache_mode", "ram")),
        lasso_thumbnail_workers=_clamped_int(payload.get("lasso_thumbnail_workers"), 8, 1, 12),
        lasso_thumbnail_display_size=_clamped_int(payload.get("lasso_thumbnail_display_size"), 132, 90, 240),
        pair_check_workers=_clamped_int(payload.get("pair_check_workers"), 8, 1, 16),
        exiftool_read_batch_size=_clamped_int(payload.get("exiftool_read_batch_size"), 20, 1, 200),
        exiftool_parallel_batches=_clamped_int(payload.get("exiftool_parallel_batches"), 1, 1, 8),
        tools=tools,
        diff_tools=diff_tools,
        images=images,
        videos=videos,
        metadata=metadata,
        gps_repair=gps_repair,
        maps=maps,
    )


def _json_default(value: Any) -> Any:
    """Serialize dataclass and datetime values for JSON."""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Cannot serialize {type(value)!r}")


def _write_atomic(path: Path, payload: str) -> None:
    """Write text via a temp file + rename so a crash cannot truncate the target."""
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(payload, encoding="utf-8")
    os.replace(temp_path, path)


def save_settings(settings: AppSettings) -> None:
    """Persist settings to disk."""
    payload = json.dumps(settings, default=_json_default, indent=2)
    try:
        directory = settings_dir()
        directory.mkdir(parents=True, exist_ok=True)
        _write_atomic(settings_path(), payload)
    except OSError:
        path = fallback_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(path, payload)
