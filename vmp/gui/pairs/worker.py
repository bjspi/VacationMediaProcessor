"""Background decode + containment confirmation worker for the pair cleanup overlay."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage

from ..common.thumbnails import decode_thumb
from ...pair_cleanup import (
    PairCandidate,
    confirm_contained,
    confirm_contained_from_arrays,
)

LOGGER = logging.getLogger("vmp.gui.pairs.worker")

THUMB_SIZE = 200

def _qimage_to_gray(image: QImage | None):
    """Convert a decoded QImage to a float64 grayscale numpy array (or None).

    Lets the containment check reuse the pixels already decoded for the preview,
    so each source image is decoded only once (HEIC decode dominates the cost).
    """
    if image is None or image.isNull():
        return None
    try:
        import numpy as np

        gray = image.convertToFormat(QImage.Format.Format_Grayscale8)
        w, h = gray.width(), gray.height()
        ptr = gray.constBits()
        ptr.setsize(gray.sizeInBytes())
        # Account for per-row padding (bytesPerLine) before cropping to width.
        arr = np.frombuffer(ptr, np.uint8).reshape((h, gray.bytesPerLine()))[:, :w]
        return arr.astype(np.float64)
    except Exception:  # noqa: BLE001
        LOGGER.debug("QImage->gray conversion failed", exc_info=True)
        return None



def _pair_dims(pair: PairCandidate, path: Path) -> tuple[int, int] | None:
    """Return the metadata (width, height) for one side of a pair, if known."""
    for result in (pair.base, pair.edit):
        if result.item.path == path and result.width and result.height:
            return int(result.width), int(result.height)
    return None



class PairWorker(QObject):
    """Background decoder + containment confirmer for the pair list."""

    thumb_ready = pyqtSignal(int, str, object)  # row, side ("big"/"small"), QImage|None
    confirm_ready = pyqtSignal(int, bool, float)  # row, contained, ncc
    progressed = pyqtSignal(int, int)  # done, total
    finished = pyqtSignal()

    def __init__(self, pairs: list[PairCandidate], ffmpeg: str | None, workers: int = 8) -> None:
        super().__init__()
        self._pairs = pairs
        self._ffmpeg = ffmpeg
        self._workers = max(1, min(16, int(workers)))
        self._stop = False
        self._done = 0
        self._lock = threading.Lock()

    def stop(self) -> None:
        """Signal the worker to stop after the current item."""
        self._stop = True

    def run(self) -> None:
        """Decode thumbnails and confirm containment for every pair in parallel.

        Each pair is processed by a thread-pool worker. PIL decode and the numpy
        containment correlation release the GIL for their heavy sections, so several
        cores are used concurrently. Qt signals are emitted from the pool threads;
        they marshal to the GUI thread via queued connections.
        """
        total = len(self._pairs)
        if total == 0:
            self.finished.emit()
            return
        try:
            with ThreadPoolExecutor(max_workers=self._workers) as pool:
                for row, pair in enumerate(self._pairs):
                    future = pool.submit(self._process_one, row, pair, total)
                    future.add_done_callback(self._log_worker_failure)
        finally:
            self.finished.emit()

    @staticmethod
    def _log_worker_failure(future) -> None:
        """Surface unexpected worker exceptions instead of dropping them silently."""
        exc = future.exception()
        if exc is not None:
            LOGGER.warning("Pair worker task failed: %s", exc, exc_info=exc)

    def _process_one(self, row: int, pair: PairCandidate, total: int) -> None:
        if self._stop:
            return
        try:
            big = decode_thumb(pair.bigger_path, "image", self._ffmpeg, THUMB_SIZE)
            self.thumb_ready.emit(row, "big", big)
            if self._stop:
                return
            small = decode_thumb(pair.smaller_path, "image", self._ffmpeg, THUMB_SIZE)
            self.thumb_ready.emit(row, "small", small)
            if pair.is_crop and not self._stop:
                try:
                    # Reuse the pixels already decoded for the preview instead of
                    # decoding both source images a second time. Original dims let
                    # the containment check derive the correct template scale.
                    big_gray = _qimage_to_gray(big)
                    small_gray = _qimage_to_gray(small)
                    if big_gray is not None and small_gray is not None:
                        contained, ncc = confirm_contained_from_arrays(
                            big_gray,
                            small_gray,
                            _pair_dims(pair, pair.bigger_path),
                            _pair_dims(pair, pair.smaller_path),
                        )
                    else:
                        contained, ncc = confirm_contained(pair.bigger_path, pair.smaller_path)
                except Exception:  # noqa: BLE001
                    LOGGER.debug("Containment check failed for %s", pair.smaller_path, exc_info=True)
                    contained, ncc = False, -1.0
                self.confirm_ready.emit(row, contained, ncc)
        finally:
            with self._lock:
                self._done += 1
                done = self._done
            self.progressed.emit(done, total)


