"""Side-by-side full-image viewer for one IMG_/IMG_E pair."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PyQt6.QtCore import QByteArray, QObject, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ...core.i18n import tr
from ..common.file_transfer import open_in_default_app
from .row import PairRow
from ..common.thumbnails import decode_thumb
from ...pair_cleanup import PairCandidate

LOGGER = logging.getLogger("vmp.gui.pairs.viewer")

class _ViewerImageRelay(QObject):
    """Delivers the two decoded viewer images back to the GUI thread."""

    loaded = pyqtSignal(object, object)  # big QImage | None, small QImage | None



class PairViewer(QDialog):
    """A side-by-side viewer showing both full images of a pair.

    Uses the same box logic as the overview thumbnails: the box aspect follows the
    bigger (original) image so it fills edge-to-edge without black bars, while the
    smaller crop is shown inside an equally sized box with black margins only where
    content was cropped away.
    """

    def __init__(
        self,
        parent: QWidget | None,
        pair: PairCandidate,
        ffmpeg: str | None,
        geometry: str = "",
        on_keep_only=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(
            tr("Vergleich: {big} ↔ {small}").format(big=pair.bigger_path.name, small=pair.smaller_path.name)
        )
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )
        self.resize(1280, 760)
        self._pair = pair
        self._on_keep_only = on_keep_only

        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        layout.addLayout(row, 1)

        self._big_img: QImage | None = None
        self._small_img: QImage | None = None
        for label_text, path in ((tr("Original"), pair.bigger_path), (tr("Bearbeitet"), pair.smaller_path)):
            col = QVBoxLayout()
            head = QLabel(f"{label_text}: {path.name}")
            head.setAlignment(Qt.AlignmentFlag.AlignCenter)
            view = QLabel(tr("lädt…"))
            view.setAlignment(Qt.AlignmentFlag.AlignCenter)
            view.setMinimumSize(200, 200)
            open_btn = QPushButton(tr("Im Standardprogramm öffnen"))
            open_btn.clicked.connect(lambda _=False, p=path: open_in_default_app(p))
            col.addWidget(head)
            # "Keep only this" only makes sense for portrait/blur pairs (two
            # genuinely different renders). For a crop the original always
            # contains the crop, so deleting it is never offered here.
            if not pair.is_crop:
                keep_btn = QPushButton(tr("Nur dieses Bild behalten"))
                keep_btn.setStyleSheet("font-weight:700;")
                keep_btn.clicked.connect(lambda _=False, p=path: self._keep_only(p))
                col.addWidget(keep_btn)
            col.addWidget(view, 1)
            col.addWidget(open_btn)
            row.addLayout(col, 1)
            if path == pair.bigger_path:
                self._big_view = view
            else:
                self._small_view = view
        if geometry:
            try:
                self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
            except Exception:  # noqa: BLE001
                LOGGER.debug("Could not restore pair viewer geometry", exc_info=True)
        # Decode both full images off the GUI thread; two large HEICs would
        # otherwise block the event loop for seconds behind the "lädt…" labels.
        self._image_relay = _ViewerImageRelay()
        self._image_relay.loaded.connect(self._on_images_loaded)
        threading.Thread(
            target=self._decode_worker,
            args=(pair.bigger_path, pair.smaller_path, ffmpeg),
            daemon=True,
        ).start()

    def _decode_worker(self, big_path: Path, small_path: Path, ffmpeg: str | None) -> None:
        big = decode_thumb(big_path, "image", ffmpeg, 2000)
        small = decode_thumb(small_path, "image", ffmpeg, 2000)
        self._image_relay.loaded.emit(big, small)

    def _on_images_loaded(self, big: object, small: object) -> None:
        self._big_img = big if isinstance(big, QImage) and not big.isNull() else None
        self._small_img = small if isinstance(small, QImage) and not small.isNull() else None
        if self._big_img is None:
            self._big_view.setText(tr("kein Bild"))
        if self._small_img is None:
            self._small_view.setText(tr("kein Bild"))
        self._render()

    def result_geometry(self) -> str:
        """Return the current window geometry (size/pos/maximized) as base64."""
        return bytes(self.saveGeometry().toBase64()).decode("ascii")

    def _keep_only(self, path: Path) -> None:
        """Mark the pair to keep only ``path`` (delete the other) and close the viewer."""
        if self._on_keep_only is not None:
            self._on_keep_only(path)
        self.accept()

    def _meta_for(self, path: Path) -> tuple[int, int] | None:
        for result in (self._pair.base, self._pair.edit):
            if result.item.path == path and result.width and result.height:
                return int(result.width), int(result.height)
        return None

    def _render(self) -> None:
        big_img, small_img = self._big_img, self._small_img
        if big_img is None:
            return  # still loading, or failed — the label text reflects it
        # Common box sized to the bigger image's aspect, fit into the space both
        # panels can offer, so the two images line up and the original never bars.
        avail_w = min(self._big_view.width(), self._small_view.width()) - 8
        avail_h = min(self._big_view.height(), self._small_view.height()) - 8
        if avail_w < 20 or avail_h < 20:
            return
        bw, bh = big_img.width(), big_img.height()
        scale = min(avail_w / bw, avail_h / bh)
        box_w, box_h = max(1, round(bw * scale)), max(1, round(bh * scale))

        self._big_view.setPixmap(
            QPixmap.fromImage(big_img).scaled(
                box_w, box_h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
        )

        if small_img is None:
            return
        canvas = QPixmap(box_w, box_h)
        canvas.fill(QColor(17, 17, 17))
        big_disp = PairRow._display_dims(self._meta_for(self._pair.bigger_path), big_img)
        small_disp = PairRow._display_dims(self._meta_for(self._pair.smaller_path), small_img)
        if big_disp and small_disp and big_disp[0] and big_disp[1]:
            target_w = max(1, min(box_w, round(box_w * small_disp[0] / big_disp[0])))
            target_h = max(1, min(box_h, round(box_h * small_disp[1] / big_disp[1])))
        else:
            target_w, target_h = box_w, box_h
        scaled = QPixmap.fromImage(small_img).scaled(
            target_w, target_h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        painter = QPainter(canvas)
        painter.drawPixmap((box_w - scaled.width()) // 2, (box_h - scaled.height()) // 2, scaled)
        painter.end()
        self._small_view.setPixmap(canvas)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._render()


