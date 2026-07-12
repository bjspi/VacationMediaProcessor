"""Worker-thread move/copy of selected media (keeps the Lasso dialog responsive)."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from ..common.file_transfer import perform_transfer


class TransferWorker(QObject):
    """Moves/copies the selected media on a worker thread (GUI stays responsive)."""

    progressed = pyqtSignal(int, int)  # done, total
    finished = pyqtSignal(object, object)  # moved: list[Path], errors: list[(Path, str)]

    def __init__(self, sources: list[Path], dest_dir: Path, copy: bool) -> None:
        super().__init__()
        self._sources = sources
        self._dest_dir = dest_dir
        self._copy = copy

    def run(self) -> None:
        moved: list[Path] = []
        errors: list[tuple[Path, str]] = []
        total = len(self._sources)
        for index, source in enumerate(self._sources, start=1):
            file_moved, file_errors = perform_transfer([source], self._dest_dir, self._copy)
            moved.extend(file_moved)
            errors.extend(file_errors)
            self.progressed.emit(index, total)
        self.finished.emit(moved, errors)


