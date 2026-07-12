"""Background preview decoding for the main window's side panel."""

from __future__ import annotations

import logging
import threading
from typing import Callable

from PyQt6.QtGui import QImage, QPixmap

from ...core.i18n import tr
from ..common.thumbnails import (
    HEIC_SUFFIXES,
    VIDEO_SUFFIXES,
    heic_to_qimage,
    load_oriented_qimage,
    video_frame_thumb,
)
from ..common.widgets import AspectRatioPreview, PreviewRelay
from ...core.models import MediaKind, MediaPlan

LOGGER = logging.getLogger("vmp.gui.main.preview_pane")

_PREVIEWABLE_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
# Cap for decoded preview images; the AspectRatioPreview label rescales to its
# box anyway, so full 48 MP decodes would only cost time and memory.
_PREVIEW_DECODE_SIZE = 2048
_VIDEO_FRAME_SIZE = 1280


class PreviewController:
    """Decodes media previews off the GUI thread and feeds the preview label."""

    def __init__(self, preview: AspectRatioPreview, get_ffmpeg: Callable[[], str]) -> None:
        self._preview = preview
        self._get_ffmpeg = get_ffmpeg
        self._token = 0
        self._relay = PreviewRelay()
        self._relay.ready.connect(self._on_ready)

    def clear(self, placeholder: str = "Preview") -> None:
        """Drop any pending decode and reset the preview to a placeholder."""
        self._token += 1
        self._preview.set_source_pixmap(None)
        self._preview.set_placeholder(placeholder)

    def request(self, plan: MediaPlan) -> None:
        """Show the preview for a plan; the image decodes in the background."""
        item = plan.analysis.item
        self._token += 1
        token = self._token
        previewable = (
            item.kind == MediaKind.IMAGE and item.suffix in _PREVIEWABLE_IMAGE_SUFFIXES
        ) or (
            item.kind == MediaKind.VIDEO and item.suffix in VIDEO_SUFFIXES
        )
        self._preview.set_source_pixmap(None)
        if not previewable:
            self._preview.set_placeholder(item.path.name)
            return
        self._preview.set_placeholder(tr("Vorschau wird geladen …"))
        threading.Thread(target=self._worker, args=(token, plan), daemon=True).start()

    def _worker(self, token: int, plan: MediaPlan) -> None:
        """Decode the preview image off the GUI thread, then hand it back."""
        image = None
        try:
            image = self._decode(plan)
        except Exception:  # noqa: BLE001
            LOGGER.warning("Preview decode failed for %s", plan.analysis.item.path, exc_info=True)
        self._relay.ready.emit(token, image)

    def _on_ready(self, token: int, image: object) -> None:
        """Apply a decoded preview image if the selection has not changed."""
        if token != self._token:
            return  # selection changed while decoding; drop the stale result
        if isinstance(image, QImage) and not image.isNull():
            self._preview.set_source_pixmap(QPixmap.fromImage(image))
        else:
            self._preview.set_source_pixmap(None)
            self._preview.set_placeholder(tr("Keine Vorschau verfügbar"))

    def _decode(self, plan: MediaPlan) -> QImage | None:
        """Return a preview QImage for an image or video plan.

        HEIC/HEIF are decoded via pillow-heif (bundled libheif), which already
        applies the stored rotation, with an ffmpeg fallback. JPEG/PNG are loaded
        via QImageReader with auto-transform so Qt applies the file's own EXIF
        orientation. Videos get a single ffmpeg frame grab.
        """
        item = plan.analysis.item
        if item.kind == MediaKind.IMAGE and item.suffix in _PREVIEWABLE_IMAGE_SUFFIXES:
            if item.suffix in HEIC_SUFFIXES:
                image = heic_to_qimage(item.path, _PREVIEW_DECODE_SIZE)
                if image is None or image.isNull():
                    image = video_frame_thumb(item.path, self._get_ffmpeg(), _VIDEO_FRAME_SIZE, seek=None)
                return image
            return load_oriented_qimage(item.path, max_size=_PREVIEW_DECODE_SIZE)
        if item.kind == MediaKind.VIDEO and item.suffix in VIDEO_SUFFIXES:
            return video_frame_thumb(item.path, self._get_ffmpeg(), _VIDEO_FRAME_SIZE, seek="1")
        return None
