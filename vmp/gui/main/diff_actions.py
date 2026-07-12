"""Backup lookup and diff-tool launching for processed media (main window mixin)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import QMessageBox

from ...core.discovery import normalize_root
from ...core.i18n import tr
from ..common.backup_discovery import discover_backup_path_for_plan
from ...core.logging_config import get_logger
from ...core.models import MediaKind, MediaPlan, PlanStatus
from ...core.processes import expand_command_template, launch_command_template, run_process

LOGGER = get_logger(__name__)


class DiffActionsMixin:
    """Diff templates, backup path resolution, and EXIF text diffs for MainWindow."""

    def open_readback_diff(self) -> None:
        """Open the latest Before/After EXIF JSONs in the configured text diff tool."""
        if self._readback_diff_paths is None:
            QMessageBox.information(self, tr("Before/After-Diff"), tr("Noch kein erfolgreicher Before/After-JSON-Lauf vorhanden."))
            return
        before_path, after_path = self._readback_diff_paths
        if not before_path.exists() or not after_path.exists():
            self._set_readback_diff_paths(None)
            QMessageBox.warning(self, tr("Before/After-Diff"), tr("Die Before/After-JSON-Dateien wurden nicht gefunden."))
            return
        template = self.settings_model.diff_tools.text.strip()
        if not template:
            QMessageBox.information(self, tr("Before/After-Diff"), tr("Textdiff ist nicht konfiguriert."))
            return
        try:
            launch_command_template(template, source=before_path, target=after_path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, tr("Before/After-Diff"), tr("Konnte Textdiff nicht starten:\n{error}").format(error=exc))

    def _set_readback_diff_paths(self, paths: tuple[Path, Path] | None) -> None:
        """Store and expose the latest successful Before/After JSON pair."""
        self._readback_diff_paths = paths
        self.readback_diff_button.setEnabled(paths is not None)
        if paths is None:
            self.readback_diff_button.setToolTip(tr("Before/After-JSON mit Textdiff öffnen"))
            return
        before_path, after_path = paths
        self.readback_diff_button.setToolTip(
            tr("Before/After-JSON mit Textdiff öffnen:\nLinks: {before_path}\nRechts: {after_path}").format(before_path=before_path, after_path=after_path)
        )

    def _readback_manifest_paths_for_run(self, run_id: str) -> tuple[Path, Path] | None:
        """Return the expected Before/After JSON paths for an apply run when they exist."""
        if self.root is None:
            return None
        manifest_dir = normalize_root(self.root) / "_VacationMediaProcessor_Manifest"
        before_path = manifest_dir / f"{run_id}_before.json"
        after_path = manifest_dir / f"{run_id}_after.json"
        if before_path.exists() and after_path.exists():
            return before_path, after_path
        return None

    def _backup_path_for_plan(self, plan: MediaPlan) -> Path | None:
        """Return the backup path for a plan when backups were created."""
        current = plan.analysis.item.path.resolve()
        backup = self._backup_paths.get(current)
        if backup is not None and backup.exists():
            return backup
        backup = discover_backup_path_for_plan(plan)
        if backup is not None:
            self._backup_paths[current] = backup
        return backup

    def _refresh_backup_paths(self, run_id: str) -> None:
        """Map post-apply files back to their backup copies."""
        if self.settings_model.skip_backup:
            self._backup_paths.clear()
            return
        for plan in self._applied_plans:
            original = self._applied_plan_sources.get(id(plan), plan.analysis.item.path)
            root = self._applied_plan_roots.get(id(plan), normalize_root(plan.analysis.item.root))
            current = plan.final_path or plan.analysis.item.path
            backup = root / "_VacationMediaProcessor_Backup" / run_id / original.relative_to(root)
            current_key = current.resolve()
            if current_key not in self._backup_paths or not self._backup_paths[current_key].exists():
                self._backup_paths[current_key] = backup

    def _diff_template(self, plan: MediaPlan, template_kind: str | None = None) -> str:
        """Return the configured diff template for a row."""
        if template_kind == "image":
            return self.settings_model.diff_tools.image.strip()
        if template_kind == "video":
            return self.settings_model.diff_tools.video.strip()
        if template_kind == "text":
            return self.settings_model.diff_tools.text.strip()
        if plan.analysis.item.kind == MediaKind.IMAGE:
            return self.settings_model.diff_tools.image.strip()
        return self.settings_model.diff_tools.video.strip()

    def _diff_tool_display_name(self, template: str, fallback: str) -> str:
        """Return a short display name for a configured diff template."""
        if not template.strip():
            return fallback
        try:
            args = expand_command_template(template, source=Path("source"), target=Path("target"))
        except Exception:  # noqa: BLE001
            args = []
        if not args:
            return fallback
        name = Path(args[0].strip('"')).stem
        return name or fallback

    def _can_open_diff_for_plan(self, plan: MediaPlan) -> bool:
        """Return True when a plan has an existing backup for diffing."""
        if plan.analysis.status != PlanStatus.DONE:
            return False
        if not plan.analysis.item.path.exists():
            return False
        backup = self._backup_path_for_plan(plan)
        return backup is not None and backup.exists()

    def _matching_media_diff_template(self, plan: MediaPlan) -> tuple[str, str] | None:
        """Return the row's matching media diff label/template when available."""
        if not self._can_open_diff_for_plan(plan):
            return None
        if plan.analysis.item.kind == MediaKind.IMAGE:
            template = self.settings_model.diff_tools.image.strip()
            if template:
                label = tr("Difftool 1 (Bilder): {tool}").format(tool=self._diff_tool_display_name(template, tr("nicht konfiguriert")))
                return label, "image"
            return None
        template = self.settings_model.diff_tools.video.strip()
        if template:
            label = tr("Difftool 2 (Videos): {tool}").format(tool=self._diff_tool_display_name(template, tr("nicht konfiguriert")))
            return label, "video"
        return None

    def _text_diff_label_for_plan(self, plan: MediaPlan) -> str | None:
        """Return the text diff label when it is configured and a backup exists."""
        if not self._can_open_diff_for_plan(plan):
            return None
        template = self.settings_model.diff_tools.text.strip()
        if not template:
            return None
        return tr("Exiftool -> Textdiff: {tool}").format(tool=self._diff_tool_display_name(template, tr("nicht konfiguriert")))

    def _open_diff_for_row(self, row: int, template_kind: str | None = None) -> bool:
        """Open the configured diff tool for one row when a backup exists."""
        if row < 0 or row >= len(self.plans):
            return False
        plan = self.plans[row]
        if plan.analysis.status != PlanStatus.DONE or not plan.analysis.item.path.exists():
            return False
        backup = self._backup_path_for_plan(plan)
        if backup is None or not backup.exists():
            return False
        template = self._diff_template(plan, template_kind)
        if not template:
            return False
        try:
            launch_command_template(template, source=backup, target=plan.analysis.item.path)
            return True
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Diff Tool", tr("Konnte Diff-Tool nicht starten:\n{error}").format(error=exc))
            return True

    def _open_exif_text_diff_for_row(self, row: int) -> bool:
        """Read before/current EXIF snapshots and open them in the configured text diff tool."""
        if row < 0 or row >= len(self.plans):
            return False
        plan = self.plans[row]
        if plan.analysis.status != PlanStatus.DONE or not plan.analysis.item.path.exists():
            return False
        backup = self._backup_path_for_plan(plan)
        template = self.settings_model.diff_tools.text.strip()
        if backup is None or not backup.exists() or not template:
            return False
        try:
            before_path, after_path = self._write_row_exif_snapshots(plan, backup)
            launch_command_template(template, source=before_path, target=after_path)
            return True
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Could not launch ExifTool text diff for row=%s", row)
            QMessageBox.warning(self, tr("ExifTool -> Textdiff"), tr("Konnte Textdiff nicht starten:\n{error}").format(error=exc))
            return True

    def _write_row_exif_snapshots(self, plan: MediaPlan, backup: Path) -> tuple[Path, Path]:
        """Write fresh ExifTool JSON snapshots for backup/current files."""
        root = normalize_root(plan.analysis.item.root)
        temp_dir = root / "_VacationMediaProcessor_Temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        base = self._safe_temp_stem(plan.analysis.item.path)
        before_path = temp_dir / f"{stamp}_{base}_before_exif.json"
        after_path = temp_dir / f"{stamp}_{base}_after_exif.json"
        self._write_exif_snapshot(backup, before_path)
        self._write_exif_snapshot(plan.analysis.item.path, after_path)
        return before_path, after_path

    def _write_exif_snapshot(self, source: Path, target: Path) -> None:
        """Read a broad ExifTool JSON payload for one source file."""
        args = [
            self.settings_model.tools.exiftool,
            "-j",
            "-a",
            "-G1",
            "-s",
            "-all:all",
            str(source),
        ]
        result = run_process(args)
        payload = json.loads(result.stdout)
        target.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    @staticmethod
    def _safe_temp_stem(path: Path) -> str:
        """Return a filesystem-friendly temp filename stem."""
        text = path.stem or "media"
        return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in text)[:80]

