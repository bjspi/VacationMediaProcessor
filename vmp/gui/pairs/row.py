"""One review row of the pair cleanup overlay: thumbnails, badge, keep/delete controls."""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QVBoxLayout

from ...core.i18n import tr
from ..common.file_transfer import open_in_default_app
from ..common.plan_display import human_size
from .worker import THUMB_SIZE
from ...pair_cleanup import PairCandidate

LOGGER = logging.getLogger("vmp.gui.pairs.row")



class _ClickableThumb(QLabel):
    """Thumbnail label: a click opens the side-by-side viewer."""

    def __init__(self, on_click) -> None:
        super().__init__("…")
        self._on_click = on_click
        self.setToolTip(tr("Klick: beide Bilder groß nebeneinander vergleichen"))
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton and self._on_click is not None:
            self._on_click()
        super().mouseReleaseEvent(event)



class _LinkLabel(QLabel):
    """A filename label that opens its file in the OS default app on click."""

    def __init__(self, text: str, path: Path) -> None:
        super().__init__(text)
        self._path = path
        self.setToolTip(tr("Klick: im Standardprogramm öffnen\n{name}").format(name=path.name))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("color:#2563eb; text-decoration:underline;")

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            open_in_default_app(self._path)
        super().mouseReleaseEvent(event)



class PairRow(QFrame):
    """One row: original + edited thumbnails, info, and per-kind keep/delete controls."""

    def __init__(self, pair: PairCandidate, on_open_viewer=None) -> None:
        super().__init__()
        self.pair = pair
        self._on_open_viewer = on_open_viewer
        self.setObjectName("pairRow")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)

        # Both preview boxes share the *bigger* image's aspect ratio, so the
        # original fills its box edge-to-edge (no letterboxing) and the smaller
        # crop sits inside an identically sized box — the black bars then mark
        # exactly what was cropped away.
        self._big_dims = self._dims_of(pair.bigger_path)
        self._small_dims = self._dims_of(pair.smaller_path)
        self._box = self._box_size(self._big_dims)
        self._big_img: QImage | None = None
        self._small_img: QImage | None = None

        open_viewer = (lambda: on_open_viewer(pair)) if on_open_viewer is not None else None
        self.big_thumb = _ClickableThumb(open_viewer)
        self.small_thumb = _ClickableThumb(open_viewer)
        for thumb in (self.big_thumb, self.small_thumb):
            thumb.setFixedSize(self._box[0], self._box[1])
            thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            thumb.setStyleSheet("background:#111; color:#888; border-radius:6px;")

        big_col = QVBoxLayout()
        big_col.addWidget(_LinkLabel(tr("Original: {name}").format(name=pair.bigger_path.name), pair.bigger_path))
        big_col.addWidget(self.big_thumb)
        small_col = QVBoxLayout()
        small_col.addWidget(_LinkLabel(tr("Bearbeitet: {name}").format(name=pair.smaller_path.name), pair.smaller_path))
        small_col.addWidget(self.small_thumb)

        info_col = QVBoxLayout()
        badge = tr("CROP (Ausschnitt)") if pair.is_crop else "PORTRAIT / BLUR"
        badge_color = "#16a34a" if pair.is_crop else "#d97706"
        self.badge = QLabel(badge)
        self.badge.setStyleSheet(f"color:#fff; background:{badge_color}; padding:2px 8px; border-radius:8px; font-weight:700;")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dims = QLabel(self._dims_text())
        self.status = QLabel(tr("prüfe…") if pair.is_crop else tr("anderer Look – kein Ausschnitt"))
        self.status.setWordWrap(True)
        info_col.addWidget(self.badge)
        info_col.addWidget(self.dims)
        info_col.addWidget(self.status)
        info_col.addStretch(1)

        # Crop pairs: a single "delete the redundant crop" checkbox (pre-selected
        # once containment is confirmed). Portrait/blur pairs are two genuinely
        # different renders, so instead offer per-file "keep" toggles: unchecking a
        # box marks that file for deletion; leaving both checked keeps both.
        if pair.is_crop:
            self.checkbox = QCheckBox(tr("kleinere Version löschen"))
            self.checkbox.setChecked(False)  # enabled once containment confirmed
            info_col.addWidget(self.checkbox)
        else:
            self.keep_original = QCheckBox(tr("Original behalten"))
            self.keep_original.setChecked(True)
            self.keep_edited = QCheckBox(tr("Bearbeitetes (Blur) behalten"))
            self.keep_edited.setChecked(True)
            info_col.addWidget(self.keep_original)
            info_col.addWidget(self.keep_edited)

        layout.addLayout(big_col)
        layout.addLayout(small_col)
        layout.addLayout(info_col, 1)

    def _dims_text(self) -> str:
        b, e = self.pair.base, self.pair.edit
        parts = []
        if b.width and b.height:
            parts.append(f"Orig {b.width}×{b.height}")
        if e.width and e.height:
            parts.append(f"Edit {e.width}×{e.height}")
        try:
            parts.append(tr("→ {size} löschbar").format(size=human_size(self.pair.smaller_path.stat().st_size)))
        except OSError:
            pass
        return "  ·  ".join(parts)

    def _dims_of(self, path: Path) -> tuple[int, int] | None:
        """Return (width, height) for one side of the pair, if known."""
        for result in (self.pair.base, self.pair.edit):
            if result.item.path == path and result.width and result.height:
                return int(result.width), int(result.height)
        return None

    def _box_size(self, dims: tuple[int, int] | None) -> tuple[int, int]:
        """Return the preview box (w, h) matching the bigger image's aspect ratio."""
        if not dims:
            return THUMB_SIZE, THUMB_SIZE
        w, h = dims
        if w >= h:
            return THUMB_SIZE, max(1, round(THUMB_SIZE * h / w))
        return max(1, round(THUMB_SIZE * w / h)), THUMB_SIZE

    def set_thumb(self, side: str, image: QImage | None) -> None:
        """Store a decoded thumbnail and (re)render both previews.

        The preview box is derived from the *decoded* bigger image, which already has
        its EXIF orientation applied — the stored metadata width/height can be the
        un-rotated sensor dimensions and would otherwise give a box with the wrong
        aspect (black bars) for rotated photos.
        """
        if side == "big":
            self._big_img = image
        else:
            self._small_img = image
        if image is None or image.isNull():
            (self.big_thumb if side == "big" else self.small_thumb).setText(tr("kein Vorschaubild"))

        # The bigger image defines the box aspect ratio, so recompute once it arrives.
        if self._big_img is not None and not self._big_img.isNull():
            w, h = self._big_img.width(), self._big_img.height()
            if w > 0 and h > 0:
                box = (THUMB_SIZE, max(1, round(THUMB_SIZE * h / w))) if w >= h \
                    else (max(1, round(THUMB_SIZE * w / h)), THUMB_SIZE)
                if box != self._box:
                    self._box = box
                    self.big_thumb.setFixedSize(*box)
                    self.small_thumb.setFixedSize(*box)
        self._render()

    @staticmethod
    def _display_dims(meta: tuple[int, int] | None, image: QImage | None) -> tuple[int, int] | None:
        """Return metadata dims re-oriented to match the decoded image's orientation."""
        if not meta:
            return None
        w, h = meta
        if image is not None and not image.isNull() and image.width() > 0 and image.height() > 0:
            if (h > w) != (image.height() > image.width()):
                return h, w
        return w, h

    def _render(self) -> None:
        """Render both thumbnails: the original fills the box, the smaller sits inside it."""
        box_w, box_h = self._box
        if self._big_img is not None and not self._big_img.isNull():
            self.big_thumb.setPixmap(
                QPixmap.fromImage(self._big_img).scaled(
                    box_w, box_h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
                )
            )
        if self._small_img is not None and not self._small_img.isNull():
            canvas = QPixmap(box_w, box_h)
            canvas.fill(QColor(17, 17, 17))
            big_disp = self._display_dims(self._big_dims, self._big_img)
            small_disp = self._display_dims(self._small_dims, self._small_img)
            if big_disp and small_disp and big_disp[0] and big_disp[1]:
                bw, bh = big_disp
                sw, sh = small_disp
                target_w = max(1, min(box_w, round(box_w * sw / bw)))
                target_h = max(1, min(box_h, round(box_h * sh / bh)))
            else:
                target_w, target_h = box_w, box_h
            scaled = QPixmap.fromImage(self._small_img).scaled(
                target_w, target_h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            painter = QPainter(canvas)
            painter.drawPixmap((box_w - scaled.width()) // 2, (box_h - scaled.height()) // 2, scaled)
            painter.end()
            self.small_thumb.setPixmap(canvas)

    def set_confirmed(self, contained: bool, ncc: float) -> None:
        """Reflect the containment result and pre-select safe crops."""
        if not self.pair.is_crop:
            return  # the delete checkbox exists only on crop rows
        if contained:
            self.status.setText(tr("✓ vollständig im Original enthalten (NCC {ncc:.2f})").format(ncc=ncc))
            self.status.setStyleSheet("color:#16a34a;")
            self.checkbox.setChecked(True)
        else:
            self.status.setText(tr("⚠ nicht sicher enthalten (NCC {ncc:.2f}) – manuell prüfen").format(ncc=ncc))
            self.status.setStyleSheet("color:#d97706;")
            self.checkbox.setChecked(False)

    def paths_to_delete(self) -> list[Path]:
        """Return the file(s) the user marked for deletion in this row."""
        if self.pair.is_crop:
            return [self.pair.smaller_path] if self.checkbox.isChecked() else []
        targets: list[Path] = []
        if not self.keep_original.isChecked():
            targets.append(self.pair.bigger_path)
        if not self.keep_edited.isChecked():
            targets.append(self.pair.smaller_path)
        return targets

    def keep_only(self, keep_path: Path) -> None:
        """Keep only ``keep_path`` and delete the other file (portrait/blur pairs only).

        Called from the side-by-side viewer's "keep only this" buttons, which are
        offered only for portrait/blur pairs; crop pairs never delete the original.
        """
        if self.pair.is_crop:
            return
        self.keep_original.setChecked(keep_path == self.pair.bigger_path)
        self.keep_edited.setChecked(keep_path == self.pair.smaller_path)

    def clear_deletion(self) -> None:
        """Reset the row to keep everything (delete nothing)."""
        if self.pair.is_crop:
            self.checkbox.setChecked(False)
        else:
            self.keep_original.setChecked(True)
            self.keep_edited.setChecked(True)


