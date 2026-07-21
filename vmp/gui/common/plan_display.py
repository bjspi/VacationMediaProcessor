"""Human-readable display strings for plans: sizes, codec cells, details panel."""

from __future__ import annotations

from math import isfinite
from pathlib import Path

from ...core.i18n import tr
from ...core.models import AnalysisResult, AppSettings, MediaKind, MediaPlan
from ...planner import crf_for_video, video_bucket_label
from ...reports import display_video_codec, resolution_class


def human_size(size: float) -> str:
    """Return a human-readable file size from bytes."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"



def file_size_text(path: "Path") -> str:
    """Return a human-readable size for a file, or empty when unreadable."""
    try:
        return human_size(path.stat().st_size)
    except OSError:
        return ""



def details_markdown(plan: MediaPlan, bucket: str) -> str:
    """Build a Markdown-formatted details/action panel for one plan."""
    result = plan.analysis
    resolved = result.resolved

    def fmt_dt(value) -> str:
        return value.strftime("%Y-%m-%d %H:%M:%S") if value else "—"

    lines = [
        f"## {result.item.path.name}",
        "",
        f"**Status:** {result.status.value}  ",
    ]
    if bucket:
        lines.append(tr("**Video-Bucket:** {bucket}  ").format(bucket=bucket))
    lines += [
        tr("**Aufnahme (Local):** {value}  ").format(value=fmt_dt(resolved.local_dt)),
        f"**UTC:** {fmt_dt(resolved.utc_dt)}  ",
        f"**Offset:** {resolved.offset if resolved.offset is not None else '—'}  ",
        f"**Confidence:** {resolved.confidence.value}  ",
        tr("**Quelle:** {value}  ").format(value=resolved.source or "—"),
        tr("**Ziel:** {value}").format(value=plan.final_path.name if plan.final_path else "—"),
        "",
        tr("### Aktionen"),
    ]
    if plan.actions:
        lines += [
            f"{idx}. **{action.kind.value}** — {action.description}"
            for idx, action in enumerate(plan.actions, start=1)
        ]
    else:
        lines.append(tr("_Keine Aktionen geplant._"))
    if result.warnings:
        lines += ["", tr("### Warnungen")]
        lines += [f"- ⚠️ {warning}" for warning in result.warnings]
    lines += ["", f"`{result.item.path}`"]
    return "\n".join(lines)


def codec_cell_text(result: AnalysisResult) -> str:
    """Return the Codec column text with resolution and rounded frame rate.

    e.g. ``x265 [FHD · 30 fps]`` / ``x264 [4K · 60 fps]``. For
    non-videos there is no codec and the cell stays empty. If only some parts
    are known, the available values are still shown.
    """
    codec = display_video_codec(result.codec)
    res = ""
    fps = ""
    if result.item.kind == MediaKind.VIDEO:
        res = resolution_class(result.width, result.height)
        if result.fps is not None and result.fps > 0 and isfinite(result.fps):
            fps = f"{int(result.fps + 0.5)} fps"
    details = " · ".join(part for part in (res, fps) if part)
    if codec and details:
        return f"{codec} [{details}]"
    if codec:
        return codec
    if details:
        return f"[{details}]"
    return ""


def video_bucket_label_text(result: AnalysisResult, settings: AppSettings) -> str:
    """Return the visible bucket/CRF label for video plans."""
    if result.item.kind != MediaKind.VIDEO:
        return ""
    bucket = video_bucket_label(result, settings)
    crf = crf_for_video(result, settings)
    return f"{bucket} / CRF {crf}"


