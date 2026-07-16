"""Apply phase: execute prepared media plans with backups and manifests."""

from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from ..core.discovery import discover_media, normalize_root
from ..core.i18n import tr
from ..tools import (
    convert_image,
    copy_all_metadata,
    embed_gdepth,
    maintain_jpeg,
    probe_video,
    transcode_video,
    write_metadata,
)
from ..core.logging_config import add_run_file_handler, get_logger, remove_run_file_handler
from ..manifest import write_before_after_manifests, write_manifest
from ..metadata import read_metadata_batch
from ..core.models import (
    ActionKind,
    ApplyItemUpdate,
    AppSettings,
    MediaItem,
    MediaKind,
    MediaPlan,
    Phase,
    PipelineReport,
    PlanStatus,
)
from .shared import (
    ApplyItemCallback,
    CancelCallback,
    PipelineCancelled,
    PipelineError,
    ProgressCallback,
    VideoNotSmallerError,
    _cleanup_empty_generated_dirs,
    _resolve_required_tool,
    backup_dir,
    emit,
    make_run_id,
    raise_if_cancelled,
    work_dir,
)
from ..planner import crf_for_video, effective_video_bucket, video_downscale_target

LOGGER = get_logger(__name__)


@dataclass(slots=True)
class _ApplyOutcome:
    """Result of processing one plan."""

    plan: MediaPlan
    source: Path
    final_path: Path | None = None
    backup_path: Path | None = None
    changed: bool = False
    skipped: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    original_size: int | None = None
    current_size: int | None = None



def _record_outcome(report: PipelineReport, outcome: _ApplyOutcome) -> None:
    """Merge one apply outcome into the shared report."""
    # The worker owns a deep copy of the plans. Keep that copy pointed at what
    # actually exists so run/readback manifests never claim that a failed target
    # was created.
    if outcome.changed and outcome.final_path is not None:
        outcome.plan.final_path = outcome.final_path
    elif outcome.source.exists():
        outcome.plan.final_path = outcome.source
    else:
        outcome.plan.final_path = None
    if outcome.errors:
        report.errors.extend(outcome.errors)
    if outcome.warnings:
        report.warnings.extend(outcome.warnings)
    if outcome.changed:
        report.changed += 1
    else:
        report.skipped += 1



def _item_update_from_outcome(run_id: str, outcome: _ApplyOutcome) -> ApplyItemUpdate:
    """Build a GUI-safe per-file update from one apply outcome."""
    return ApplyItemUpdate(
        run_id=run_id,
        source_path=outcome.source,
        final_path=outcome.final_path,
        backup_path=outcome.backup_path,
        changed=outcome.changed,
        skipped=outcome.skipped,
        errors=outcome.errors.copy(),
        original_size=outcome.original_size,
        current_size=outcome.current_size,
    )



def _emit_item_update(callback: ApplyItemCallback | None, run_id: str, outcome: _ApplyOutcome) -> None:
    """Emit a per-file update when requested."""
    if callback is not None:
        callback(_item_update_from_outcome(run_id, outcome))


def _same_path(left: Path, right: Path) -> bool:
    """Compare two paths using resolved, Windows-friendly semantics."""
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left == right



def _apply_one_plan(
    plan: MediaPlan,
    settings: AppSettings,
    run_id: str,
    callback: ProgressCallback | None,
    index: int,
    total: int,
    video_state: dict[str, object] | None = None,
    total_video_seconds: float = 0.0,
    video_durations: dict[Path, float] | None = None,
    backup_lock: Lock | None = None,
    cancel_callback: CancelCallback | None = None,
) -> _ApplyOutcome:
    """Apply one plan and capture the result without touching shared report state."""
    source = plan.analysis.item.path
    item_root = normalize_root(plan.analysis.item.root)
    work = work_dir(item_root, run_id)
    backup = backup_dir(item_root, run_id)
    outcome = _ApplyOutcome(plan=plan, source=source)
    try:
        if cancel_callback is not None and cancel_callback():
            outcome.skipped = True
            return outcome
        try:
            outcome.original_size = source.stat().st_size
        except OSError:
            outcome.original_size = None
        LOGGER.info(
            "Apply item %s/%s source=%s final=%s actions=%s item_root=%s",
            index,
            total,
            source,
            plan.final_path,
            [action.kind.value for action in plan.actions if action.enabled],
            item_root,
        )
        final_path = plan.final_path
        if final_path is None:
            LOGGER.info("Apply item skipped because final_path is None: %s", source)
            outcome.skipped = True
            return outcome
        if not _same_path(final_path, source) and final_path.exists():
            # Planning is a snapshot. A file can appear at the chosen target
            # before Apply; reject it before metadata writes or conversions.
            raise PipelineError(f"Final path already exists, refusing to overwrite: {final_path}")
        emit(callback, Phase.VIDEO_TRANSCODE, index - 1, total, f"Preparing {source.name}")
        output = _produce_output(
            plan,
            item_root,
            work,
            settings,
            callback,
            index,
            total,
            video_state,
            total_video_seconds,
            video_durations,
        )
        if output is None:
            LOGGER.info("Apply item skipped because no output was produced: %s", source)
            outcome.skipped = True
            return outcome
        if cancel_callback is not None and cancel_callback():
            if not _same_path(output, source):
                output.unlink(missing_ok=True)
            outcome.skipped = True
            return outcome
        if settings.skip_backup:
            LOGGER.warning("Backup skipped by user setting for source=%s", source)
        else:
            if backup_lock is None:
                outcome.backup_path = _backup_source(source, item_root, backup)
            else:
                with backup_lock:
                    outcome.backup_path = _backup_source(source, item_root, backup)
        if cancel_callback is not None and cancel_callback():
            if not _same_path(output, source):
                output.unlink(missing_ok=True)
            outcome.skipped = True
            return outcome
        if output == source:
            emit(callback, Phase.METADATA_WRITE, index, total, f"Writing metadata: {source.name}")
            LOGGER.info("Writing metadata in-place source=%s final=%s", source, final_path)
            write_metadata(plan.analysis, source, settings)
            if not _same_path(final_path, source):
                final_path.parent.mkdir(parents=True, exist_ok=True)
                if final_path.exists():
                    raise PipelineError(f"Final path already exists, refusing to overwrite: {final_path}")
                LOGGER.info("Moving source to final path: %s -> %s", source, final_path)
                shutil.move(str(source), str(final_path))
        else:
            emit(callback, Phase.METADATA_WRITE, index, total, f"Copying/writing metadata: {source.name}")
            LOGGER.info("Copying metadata source=%s output=%s", source, output)
            copy_all_metadata(source, output, settings, plan.analysis.item.kind)
            LOGGER.info("Writing metadata output=%s", output)
            write_metadata(plan.analysis, output, settings)
            if (
                settings.images.preserve_depth_as_gdepth
                and plan.analysis.has_depth
                and source.suffix.lower() in {".heic", ".heif"}
                and output.suffix.lower() in {".jpg", ".jpeg"}
            ):
                embed_gdepth(source, output, settings)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            if _same_path(final_path, source):
                LOGGER.info("Replacing original with output: %s -> %s", output, final_path)
                output.replace(final_path)
            else:
                if final_path.exists():
                    # shutil.move silently overwrites on Windows; a stale file at
                    # the target (e.g. from an earlier additive session) must not
                    # be clobbered — fail this item and keep the original.
                    raise PipelineError(f"Final path already exists, refusing to overwrite: {final_path}")
                LOGGER.info("Moving output to final path: %s -> %s", output, final_path)
                shutil.move(str(output), str(final_path))
                if not final_path.exists():
                    raise PipelineError(f"Expected final file was not created: {final_path}")
                if source.exists():
                    LOGGER.info("Removing original source after replacement: %s", source)
                    try:
                        source.unlink()
                    except OSError as exc:
                        # The intended final file is already complete. Keeping an
                        # extra original is recoverable and safer than reporting
                        # the whole transformed item as if it had failed.
                        warning = f"Could not remove replaced original {source}: {exc}"
                        LOGGER.warning(warning)
                        outcome.warnings.append(warning)
        outcome.final_path = final_path
        outcome.changed = True
        try:
            outcome.current_size = final_path.stat().st_size
        except OSError:
            outcome.current_size = None
        if source in (video_durations or {}):
            if video_state is not None:
                video_state["completed"] += (video_durations or {})[source]
                LOGGER.info(
                    "Video cumulative progress: %.1fs / %.1fs",
                    video_state["completed"],
                    total_video_seconds,
                )
        LOGGER.info("Apply item completed: %s", source)
        return outcome
    except VideoNotSmallerError as exc:
        # Deliberate no-op: the re-encode did not save space, so the original is
        # kept. Count as a skip with a warning, not an error.
        LOGGER.info("Apply item kept original (not smaller): %s (%s)", source, exc)
        outcome.warnings.append(f"{source}: {exc}")
        outcome.skipped = True
        return outcome
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Apply item failed: %s", source)
        outcome.errors.append(f"{source}: {exc}")
        outcome.skipped = True
        return outcome



def apply_plans(
    root: Path,
    plans: list[MediaPlan],
    settings: AppSettings,
    callback: ProgressCallback | None = None,
    item_callback: ApplyItemCallback | None = None,
    cancel_callback: CancelCallback | None = None,
) -> PipelineReport:
    """Apply enabled plans using backup and work directories.

    Each file is backed up under its own item root, so additive multi-folder
    sessions work correctly — files from a second folder get their backup in
    that folder's _VacationMediaProcessor_Backup tree, not the primary root's.
    Image plans are processed first with bounded parallelism; video plans are
    processed afterwards, strictly serially, and the manifest is written under
    the primary root.
    """
    normalized_root = normalize_root(root)
    raise_if_cancelled(cancel_callback)
    LOGGER.info("Apply requested for root=%s incoming_plans=%s", normalized_root, len(plans))
    _preflight_apply_tools(plans, settings)
    run_id = make_run_id()
    run_log_handler = add_run_file_handler(work_dir(normalized_root, run_id) / f"vmp_run_{run_id}.log")
    report = PipelineReport(run_id=run_id)
    try:
        applicable = [plan for plan in plans if plan.analysis.status != PlanStatus.SKIP and plan.final_path is not None]
        item_roots = {normalize_root(plan.analysis.item.root) for plan in applicable}
        skipped = [plan for plan in plans if plan.analysis.status == PlanStatus.SKIP]
        LOGGER.info(
            "Apply run_id=%s applicable=%s skipped=%s primary_root=%s",
            run_id,
            len(applicable),
            len(skipped),
            normalized_root,
        )
        if settings.metadata.stop_on_conflict and skipped:
            LOGGER.error("Apply blocked: stop_on_conflict enabled and skipped=%s", len(skipped))
            raise PipelineError(
                f"Apply blocked because {len(skipped)} file(s) are unresolved and stop-on-conflict is enabled."
            )
        total = len(applicable)
        image_plans = [plan for plan in applicable if plan.analysis.item.kind == MediaKind.IMAGE]
        video_plans = [plan for plan in applicable if plan.analysis.item.kind == MediaKind.VIDEO]
        backup_lock = Lock()
        image_total = len(image_plans)

        if image_plans:
            image_workers = max(1, min(16, int(settings.images.parallel_workers), image_total))
            LOGGER.info("Image phase started plans=%s workers=%s", image_total, image_workers)
            if image_workers == 1:
                for index, plan in enumerate(image_plans, start=1):
                    outcome = _apply_one_plan(
                        plan,
                        settings,
                        run_id,
                        None,
                        index,
                        image_total,
                        backup_lock=backup_lock,
                        cancel_callback=cancel_callback,
                    )
                    _record_outcome(report, outcome)
                    _emit_item_update(item_callback, run_id, outcome)
                    raise_if_cancelled(cancel_callback)
                    emit(
                        callback,
                        Phase.IMAGE_CONVERSION,
                        index,
                        image_total,
                        tr("Bilder werden verarbeitet ({done}/{total}): {name}").format(
                            done=index, total=image_total, name=plan.analysis.item.path.name
                        ),
                    )
            else:
                with ThreadPoolExecutor(max_workers=image_workers) as executor:
                    futures = {
                        executor.submit(
                            _apply_one_plan,
                            plan,
                            settings,
                            run_id,
                            None,
                            index,
                            image_total,
                            None,
                            0.0,
                            None,
                            backup_lock,
                            cancel_callback,
                        ): plan
                        for index, plan in enumerate(image_plans, start=1)
                    }
                    completed = 0
                    for future in as_completed(futures):
                        raise_if_cancelled(cancel_callback)
                        plan = futures[future]
                        try:
                            outcome = future.result()
                        except Exception as exc:  # noqa: BLE001
                            LOGGER.exception("Image apply future crashed: %s", plan.analysis.item.path)
                            outcome = _ApplyOutcome(
                                plan=plan,
                                source=plan.analysis.item.path,
                                skipped=True,
                                errors=[f"{plan.analysis.item.path}: {exc}"],
                            )
                        _record_outcome(report, outcome)
                        _emit_item_update(item_callback, run_id, outcome)
                        completed += 1
                        emit(
                            callback,
                            Phase.IMAGE_CONVERSION,
                            completed,
                            image_total,
                            tr("Bilder werden verarbeitet ({done}/{total}): {name}").format(
                                done=completed, total=image_total, name=plan.analysis.item.path.name
                            ),
                        )
            emit(
                callback,
                Phase.IMAGE_CONVERSION,
                image_total,
                image_total,
                tr("Bilder werden verarbeitet ({done}/{total})").format(done=image_total, total=image_total),
            )

        raise_if_cancelled(cancel_callback)

        total_video_seconds = 0.0
        video_durations: dict[Path, float] = {}
        for vp in video_plans:
            raise_if_cancelled(cancel_callback)
            try:
                probe = probe_video(vp.analysis.item.path, settings)
                fmt = probe.get("format", {})
                dur = float(fmt.get("duration", 0.0)) if isinstance(fmt, dict) else 0.0
            except Exception:  # noqa: BLE001
                dur = 0.0
            video_durations[vp.analysis.item.path] = dur
            total_video_seconds += dur
        video_state = {"completed": 0.0, "speed_samples": []}
        LOGGER.info("Video progress: %s videos, total duration %.1fs", len(video_plans), total_video_seconds)

        for index, plan in enumerate(video_plans, start=1):
            outcome = _apply_one_plan(
                plan,
                settings,
                run_id,
                callback,
                index,
                len(video_plans),
                video_state,
                total_video_seconds,
                video_durations,
                backup_lock,
                cancel_callback,
            )
            _record_outcome(report, outcome)
            _emit_item_update(item_callback, run_id, outcome)
            raise_if_cancelled(cancel_callback)

        manifest_dir = normalized_root / "_VacationMediaProcessor_Manifest"
        manifest_path = manifest_dir / f"{run_id}.json"
        try:
            emit(callback, Phase.MANIFEST, total, total, "Writing run manifest...")
            LOGGER.info("Writing apply manifest: %s", manifest_path)
            write_manifest(manifest_path, root=normalized_root, settings=settings, plans=plans, report=report)
            if settings.read_after_exif:
                try:
                    after_items = [
                        MediaItem(
                            plan.final_path or plan.analysis.item.path,
                            plan.analysis.item.root,
                            plan.analysis.item.kind,
                        )
                        for plan in plans
                        if plan.final_path is not None
                    ]
                    # Chunked like the scan: one giant argv would blow the ~32k
                    # Windows command-line limit on large runs.
                    readback_chunk = max(1, min(200, int(settings.exiftool_read_batch_size)))
                    after_records = {}
                    for start in range(0, len(after_items), readback_chunk):
                        after_records.update(
                            read_metadata_batch(after_items[start : start + readback_chunk], settings.tools.exiftool)
                        )
                    before_manifest_path = manifest_dir / f"{run_id}_before.json"
                    after_manifest_path = manifest_dir / f"{run_id}_after.json"
                    LOGGER.info("Writing post-apply before/after manifests: %s / %s", before_manifest_path, after_manifest_path)
                    write_before_after_manifests(
                        before_manifest_path,
                        after_manifest_path,
                        root=normalized_root,
                        settings=settings,
                        plans=plans,
                        report=report,
                        after_metadata=after_records,
                    )
                except Exception as exc:  # noqa: BLE001
                    warning = f"Post-apply EXIF readback/before-after manifests failed: {exc}"
                    LOGGER.exception(warning)
                    report.warnings.append(warning)
                    try:
                        write_manifest(manifest_path, root=normalized_root, settings=settings, plans=plans, report=report)
                    except Exception:  # noqa: BLE001
                        LOGGER.exception("Could not update run manifest after post-apply readback failure.")
            LOGGER.info(
                "Apply completed run_id=%s changed=%s skipped=%s errors=%s",
                run_id,
                report.changed,
                report.skipped,
                len(report.errors),
            )
            return report
        finally:
            for item_root in item_roots:
                _cleanup_empty_generated_dirs(item_root, run_id)
    finally:
        remove_run_file_handler(run_log_handler)



def maintain_jpegs(
    root: Path,
    settings: AppSettings,
    callback: ProgressCallback | None = None,
    cancel_callback: CancelCallback | None = None,
) -> PipelineReport:
    """Run the standalone JPEG thumbnail/orientation maintenance workflow."""
    normalized_root = normalize_root(root)
    raise_if_cancelled(cancel_callback)
    LOGGER.info("JPEG maintenance requested for root=%s", normalized_root)
    _resolve_required_tool("NConvert", settings.tools.xnconvert)
    if not settings.images.jpeg_rotate_by_exif and not settings.images.jpeg_rebuild_exif_thumbnail:
        raise PipelineError("No JPEG maintenance option is enabled.")
    run_id = make_run_id()
    work = work_dir(normalized_root, run_id)
    backup = backup_dir(normalized_root, run_id)
    report = PipelineReport(run_id=run_id)
    items = [
        item
        for item in discover_media(normalized_root, recursive=settings.recursive)
        if item.kind == MediaKind.IMAGE and item.suffix in {".jpg", ".jpeg"}
    ]
    total = len(items)
    LOGGER.info("JPEG maintenance run_id=%s items=%s work=%s backup=%s", run_id, total, work, backup)
    emit(callback, Phase.DISCOVERY, total, total, f"Found {total} JPEG files.")
    for index, item in enumerate(items, start=1):
        raise_if_cancelled(cancel_callback)
        source = item.path
        target = work / item.relative_path
        try:
            LOGGER.info("JPEG maintenance item %s/%s source=%s target=%s", index, total, source, target)
            emit(callback, Phase.JPEG_MAINTENANCE, index - 1, total, f"Fixing JPEG: {source.name}")
            maintain_jpeg(source, target, settings)
            if not target.exists():
                raise PipelineError(f"NConvert did not create expected file: {target}")
            if settings.skip_backup:
                LOGGER.warning("Backup skipped by user setting for JPEG source=%s", source)
            else:
                _backup_source(source, normalize_root(item.root), backup)
            if cancel_callback is not None and cancel_callback():
                target.unlink(missing_ok=True)
                raise PipelineCancelled("Processing was cancelled.")
            target.replace(source)
            report.changed += 1
            LOGGER.info("JPEG maintenance item completed: %s", source)
            emit(callback, Phase.JPEG_MAINTENANCE, index, total, f"Fixed JPEG: {source.name}")
        except PipelineCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("JPEG maintenance item failed: %s", source)
            report.errors.append(f"{source}: {exc}")
            report.skipped += 1
    manifest_path = normalized_root / "_VacationMediaProcessor_Manifest" / f"{run_id}_jpeg_maintenance.json"
    try:
        emit(callback, Phase.MANIFEST, total, total, "Writing JPEG maintenance manifest...")
        LOGGER.info("Writing JPEG maintenance manifest: %s", manifest_path)
        write_manifest(manifest_path, root=normalized_root, settings=settings, plans=[], report=report)
        LOGGER.info(
            "JPEG maintenance completed run_id=%s changed=%s skipped=%s errors=%s",
            run_id,
            report.changed,
            report.skipped,
            len(report.errors),
        )
        return report
    finally:
        _cleanup_empty_generated_dirs(normalized_root, run_id)



def _preflight_apply_tools(plans: list[MediaPlan], settings: AppSettings) -> None:
    """Validate external tools needed by an apply run."""
    LOGGER.info("Apply preflight started for plans=%s", len(plans))
    _resolve_required_tool("ExifTool", settings.tools.exiftool)
    action_kinds = {action.kind for plan in plans for action in plan.actions if action.enabled}
    LOGGER.info("Apply preflight action kinds=%s", sorted(action.value for action in action_kinds))
    if ActionKind.VIDEO_TRANSCODE in action_kinds or ActionKind.VIDEO_DOWNSCALE in action_kinds:
        _resolve_required_tool("FFmpeg", settings.tools.ffmpeg)
    if ActionKind.IMAGE_CONVERT in action_kinds:
        _resolve_required_tool("XnConvert/NConvert", settings.tools.xnconvert)
    LOGGER.info("Apply preflight completed")



def _produce_output(
    plan: MediaPlan,
    root: Path,
    work: Path,
    settings: AppSettings,
    callback: ProgressCallback | None,
    index: int,
    total: int,
    video_state: dict[str, object] | None = None,
    total_video_seconds: float = 0.0,
    video_durations: dict[Path, float] | None = None,
) -> Path | None:
    """Produce a transformed output file when the plan requires it."""
    source = plan.analysis.item.path
    final_suffix = plan.final_path.suffix if plan.final_path else source.suffix
    relative = plan.analysis.item.relative_path
    # Keep the original name INCLUDING its suffix in the temp name: IMG_1234.HEIC
    # and IMG_1234.JPG in the same folder would otherwise both map to
    # work/.../IMG_1234.jpg and race in the parallel image phase.
    temp_target = work / relative.with_name(f"{relative.name}{final_suffix}")
    action_kinds = {action.kind for action in plan.actions if action.enabled}
    if ActionKind.IMAGE_CONVERT in action_kinds:
        emit(callback, Phase.IMAGE_CONVERSION, index, total, f"Converting image: {source.name}")
        LOGGER.info("Producing image output source=%s target=%s", source, temp_target)
        convert_image(source, temp_target, settings)
        if not temp_target.exists():
            raise PipelineError(f"XnConvert did not create expected file: {temp_target}")
        LOGGER.info("Image output created target=%s size=%s", temp_target, temp_target.stat().st_size)
        return temp_target
    if ActionKind.VIDEO_TRANSCODE in action_kinds:
        emit(callback, Phase.VIDEO_TRANSCODE, max(0, index - 1), total, f"Preparing video: {source.name}")
        LOGGER.info("Producing video output source=%s target=%s", source, temp_target)
        this_duration = (video_durations or {}).get(source, 0.0)

        def _video_progress(current_sec: float, total_sec: float, speed: str) -> None:
            if video_state is not None and total_video_seconds > 0:
                completed = float(video_state.get("completed", 0.0))
                overall_sec = completed + min(current_sec, this_duration)
                overall_pct = int(overall_sec / total_video_seconds * 100)
                overall_pct = min(max(overall_pct, 0), 100)
                samples = video_state.setdefault("speed_samples", [])
                if speed and speed.endswith("x"):
                    try:
                        sv = float(speed.rstrip("x"))
                        if sv > 0:
                            samples.append(sv)
                    except ValueError:
                        pass
                if samples:
                    avg_speed = sum(samples) / len(samples)
                    remaining_sec = max(0.0, total_video_seconds - overall_sec)
                    eta_sec = remaining_sec / avg_speed if avg_speed > 0 else 0
                    eta_h, eta_rem = divmod(int(eta_sec), 3600)
                    eta_m, eta_s = divmod(eta_rem, 60)
                    if eta_h:
                        eta_text = f" — ETA {eta_h}h {eta_m}m {eta_s}s"
                    else:
                        eta_text = f" — ETA {eta_m}m {eta_s}s"
                else:
                    eta_text = ""
                speed_display = f" @ {speed}" if speed else ""
                file_pct = int(current_sec / total_sec * 100) if total_sec > 0 else 0
                decile = file_pct // 10
                if decile > int(video_state.get("last_logged_decile", -1)):
                    video_state["last_logged_decile"] = decile
                    LOGGER.info(
                        "Video %s/%s %s: %s%%%s%s",
                        index,
                        total,
                        source.name,
                        file_pct,
                        speed_display,
                        eta_text,
                    )
                emit(
                    callback,
                    Phase.VIDEO_TRANSCODE,
                    overall_pct,
                    100,
                    f"Video {index}/{total}: {source.name} {file_pct}%{speed_display}{eta_text}",
                )
            else:
                pct = int(current_sec / total_sec * 100) if total_sec > 0 else 0
                speed_display = f" @ {speed}" if speed else ""
                emit(
                    callback,
                    Phase.VIDEO_TRANSCODE,
                    index,
                    total,
                    f"Transcoding {source.name}: {pct}%{speed_display}",
                )

        crf = crf_for_video(plan.analysis, settings)
        downscale = video_downscale_target(plan.analysis, settings)
        bucket = effective_video_bucket(plan.analysis, settings)
        if settings.videos.audio_codec == "copy":
            audio_desc = "copy (original)"
        else:
            audio_desc = f"{settings.videos.audio_codec} {settings.videos.audio_bitrate}"
        LOGGER.info(
            "ACTION video=%s | Transcode HEVC/x265 | Bucket=%s | Downscale->FHD=%s | CRF=%s | Preset=%s | Audio=%s",
            source.name,
            bucket,
            "ja" if downscale is not None else "nein",
            crf,
            settings.videos.preset,
            audio_desc,
        )
        if video_state is not None:
            video_state["last_logged_decile"] = -1
        transcode_video(
            source,
            temp_target,
            crf,
            settings,
            downscale,
            _video_progress,
        )
        if not temp_target.exists():
            raise PipelineError(f"FFmpeg did not create expected file: {temp_target}")
        source_size = source.stat().st_size
        target_size = temp_target.stat().st_size
        threshold_bytes = settings.videos.always_replace_below_mb * 1024 * 1024
        is_small = source_size < threshold_bytes
        if not settings.videos.replace_if_larger and target_size > source_size and not is_small:
            temp_target.unlink(missing_ok=True)
            raise VideoNotSmallerError("Encoded video is larger than the source; original kept.")
        if target_size > source_size and is_small:
            LOGGER.info("Output larger than source but below threshold (%s MB < %s MB), keeping output", source_size // (1024*1024), settings.videos.always_replace_below_mb)
        LOGGER.info("Video output created target=%s size=%s", temp_target, target_size)
        return temp_target
    LOGGER.info("No conversion/transcode needed, using source as output: %s", source)
    return source



def _backup_source(source: Path, root: Path, backup: Path) -> Path:
    """Copy an original source file into the run backup tree."""
    relative = source.relative_to(root)
    target = backup / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        index = 2
        while True:
            candidate = target.with_name(f"{target.stem}-{index}{target.suffix}")
            if not candidate.exists():
                target = candidate
                break
            index += 1
    LOGGER.info("Backing up source %s -> %s", source, target)
    shutil.copy2(source, target)
    return target

