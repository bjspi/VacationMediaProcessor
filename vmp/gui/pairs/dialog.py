"""Overlay to review and clean up iPhone ``IMG_`` / ``IMG_E`` duplicate pairs.

Shows every detected pair side by side (original vs. edited), classifies each as a
lossless **crop** or a **portrait/blur look** edit, and lets the user move the redundant
smaller version into a backup folder. Crop pairs are pixel-confirmed (the smaller image is
proven to be contained in the original) and pre-selected; look pairs are shown but never
pre-selected because the edit is a genuinely different render.

Geometry is persisted by the caller (``MainWindow``) exactly like the Trip Lasso overlay.
Thumbnails and the containment check run on a background thread so the list stays
responsive while it fills in.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from PyQt6.QtCore import QByteArray, Qt, QThread
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...core.i18n import tr
from ..common.file_transfer import unique_target
from .row import PairRow
from .viewer import PairViewer
from .worker import PairWorker
from ...pair_cleanup import PairCandidate

LOGGER = logging.getLogger("vmp.gui.pairs.dialog")

BACKUP_DIR_NAME = "_VacationMediaProcessor_PairCleanup"


class PairCleanupDialog(QDialog):
    """Review/clean-up overlay for IMG_/IMG_E pairs."""

    def __init__(
        self,
        parent: QWidget | None,
        pairs: list[PairCandidate],
        root: Path | None,
        ffmpeg: str | None,
        geometry: str = "",
        workers: int = 8,
        viewer_geometry: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("iPhone-Paare aufräumen (IMG / IMG_E)"))
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )
        self.resize(1100, 780)
        self.setModal(True)

        self._pairs = pairs
        self._root = root
        self._ffmpeg = ffmpeg
        self._workers = workers
        self._viewer_geometry = viewer_geometry
        self._rows: list[PairRow] = []
        # Public result read by the caller after exec().
        self.deleted_paths: list[Path] = []

        self._build_ui()
        self._start_worker()

        self.finished.connect(self._cleanup)
        if geometry:
            try:
                self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
            except Exception:  # noqa: BLE001
                LOGGER.debug("Could not restore pair window geometry", exc_info=True)

    # -- UI ---------------------------------------------------------------- #
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        crop_count = sum(1 for p in self._pairs if p.is_crop)
        look_count = len(self._pairs) - crop_count
        header = QLabel(
            tr(
                "{total} Paare gefunden — {crops} Crop (löschbar), "
                "{looks} Portrait/Blur (Review). Crop-Paare werden nach Pixel-Prüfung vorausgewählt."
            ).format(total=len(self._pairs), crops=crop_count, looks=look_count)
        )
        header.setWordWrap(True)
        outer.addWidget(header)

        self.progress = QProgressBar()
        self.progress.setRange(0, max(1, len(self._pairs)))
        self.progress.setFormat(tr("Vorschau/Prüfung: %v / %m"))
        outer.addWidget(self.progress)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        self._rows_layout = QVBoxLayout(container)
        self._rows_layout.setSpacing(6)
        for pair in self._pairs:
            row = PairRow(pair, on_open_viewer=self._open_viewer)
            self._rows.append(row)
            self._rows_layout.addWidget(row)
        self._rows_layout.addStretch(1)
        scroll.setWidget(container)
        outer.addWidget(scroll, 1)

        footer = QHBoxLayout()
        self.show_look_only_button = QPushButton(tr("Nur Portrait / Blur anzeigen"))
        self.show_look_only_button.clicked.connect(lambda: self._apply_filter(look_only=True))
        self.show_all_button = QPushButton(tr("Alle anzeigen"))
        self.show_all_button.clicked.connect(lambda: self._apply_filter(look_only=False))
        self.select_all_crops = QPushButton(tr("Alle Crops auswählen"))
        self.select_all_crops.clicked.connect(self._select_all_crops)
        self.select_none = QPushButton(tr("Auswahl aufheben"))
        self.select_none.clicked.connect(self._select_none)
        self.delete_button = QPushButton(tr("Ausgewählte löschen → Backup"))
        self.delete_button.setStyleSheet("font-weight:700;")
        self.delete_button.clicked.connect(self._delete_selected)
        self.close_button = QPushButton(tr("Schließen"))
        self.close_button.clicked.connect(self.reject)
        footer.addWidget(self.show_look_only_button)
        footer.addWidget(self.show_all_button)
        footer.addWidget(self.select_all_crops)
        footer.addWidget(self.select_none)
        footer.addStretch(1)
        footer.addWidget(self.delete_button)
        footer.addWidget(self.close_button)
        outer.addLayout(footer)

    def _apply_filter(self, look_only: bool) -> None:
        """Show only Portrait/Blur pairs, or all pairs again."""
        for row in self._rows:
            row.setVisible((not row.pair.is_crop) if look_only else True)

    def _open_viewer(self, pair: PairCandidate) -> None:
        """Open a side-by-side viewer with both full images of the pair."""
        row = next((r for r in self._rows if r.pair is pair), None)
        viewer = PairViewer(
            self,
            pair,
            self._ffmpeg,
            geometry=self._viewer_geometry,
            on_keep_only=(row.keep_only if row is not None else None),
        )
        viewer.exec()
        self._viewer_geometry = viewer.result_geometry()

    def viewer_geometry(self) -> str:
        """Return the last side-by-side viewer geometry, for persistence."""
        return self._viewer_geometry

    # -- background worker ------------------------------------------------- #
    def _start_worker(self) -> None:
        self._thread = QThread(self)
        self._worker = PairWorker(self._pairs, self._ffmpeg, self._workers)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.thumb_ready.connect(self._on_thumb)
        self._worker.confirm_ready.connect(self._on_confirm)
        self._worker.progressed.connect(self._on_progress)
        self._worker.finished.connect(self._thread.quit)
        self._thread.start()

    def _on_thumb(self, row: int, side: str, image: QImage | None) -> None:
        if 0 <= row < len(self._rows):
            self._rows[row].set_thumb(side, image)

    def _on_confirm(self, row: int, contained: bool, ncc: float) -> None:
        if 0 <= row < len(self._rows):
            self._rows[row].set_confirmed(contained, ncc)

    def _on_progress(self, done: int, total: int) -> None:
        self.progress.setValue(done)

    # -- selection helpers ------------------------------------------------- #
    def _select_all_crops(self) -> None:
        for row in self._rows:
            if row.pair.is_crop:
                row.checkbox.setChecked(True)

    def _select_none(self) -> None:
        for row in self._rows:
            row.clear_deletion()

    # -- deletion ---------------------------------------------------------- #
    def _backup_target(self, path: Path) -> Path:
        """Return the backup destination path, mirroring the folder structure."""
        if self._root is not None:
            try:
                rel = path.resolve().relative_to(self._root.resolve())
                dest_dir = self._root / BACKUP_DIR_NAME / rel.parent
            except (ValueError, OSError):
                dest_dir = (self._root / BACKUP_DIR_NAME)
        else:
            dest_dir = path.parent / BACKUP_DIR_NAME
        dest_dir.mkdir(parents=True, exist_ok=True)
        return unique_target(dest_dir, path.name)

    def _delete_selected(self) -> None:
        jobs = [(row, path) for row in self._rows for path in row.paths_to_delete()]
        if not jobs:
            QMessageBox.information(self, tr("Keine Auswahl"), tr("Es sind keine Dateien zum Löschen ausgewählt."))
            return
        confirm = QMessageBox.question(
            self,
            tr("Löschen bestätigen"),
            tr("{count} Datei(en) in den Backup-Ordner\n„{folder}“ verschieben?").format(
                count=len(jobs), folder=BACKUP_DIR_NAME
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        moved: list[Path] = []
        errors: list[tuple[Path, str]] = []
        touched_rows: set[PairRow] = set()
        for row, source in jobs:
            try:
                target = self._backup_target(source)
                shutil.move(str(source), str(target))
                moved.append(source)
                touched_rows.add(row)
                LOGGER.info("Moved pair duplicate %s -> %s", source, target)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Pair delete failed for %s: %s", source, exc)
                errors.append((source, str(exc)))

        for row in touched_rows:
            row.setEnabled(False)
            row.clear_deletion()
            row.status.setText(tr("→ in Backup verschoben"))

        self.deleted_paths.extend(moved)
        message = tr("{count} Datei(en) nach „{folder}“ verschoben.").format(count=len(moved), folder=BACKUP_DIR_NAME)
        if errors:
            message += "\n" + tr("{count} Fehler (siehe Log).").format(count=len(errors))
        QMessageBox.information(self, tr("Fertig"), message)

    def result_geometry(self) -> str:
        """Return the current window geometry (size/pos/maximized) as base64."""
        return bytes(self.saveGeometry().toBase64()).decode("ascii")

    def _cleanup(self) -> None:
        try:
            if hasattr(self, "_worker"):
                self._worker.stop()
            if hasattr(self, "_thread"):
                self._thread.quit()
                # The worker's run() blocks until in-flight decodes finish; a
                # QThread destroyed while still running crashes the process, so
                # keep waiting instead of giving up after one fixed timeout.
                while self._thread.isRunning() and not self._thread.wait(2000):
                    LOGGER.warning("Waiting for pair worker thread to finish before closing dialog")
        except Exception:  # noqa: BLE001
            LOGGER.debug("Pair dialog cleanup error", exc_info=True)
