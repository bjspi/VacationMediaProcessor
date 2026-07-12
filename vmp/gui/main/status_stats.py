"""Status bar statistics and header badge counts for the main window (mixin)."""

from __future__ import annotations

from ..common.plan_display import human_size
from ...core.i18n import tr
from ...core.logging_config import get_logger
from ...core.models import MediaKind
from ...pair_cleanup import find_pairs
from ...reports import missing_exif_rows

LOGGER = get_logger(__name__)


class StatusStatsMixin:
    """Debounced size/count statistics and missing-EXIF / pair badges."""

    def _schedule_stats_update(self) -> None:
        """Debounce the per-file stats refresh (one stat() per file each pass)."""
        self._stats_timer.start()

    def _update_stats(self) -> None:
        """Update the bottom status bar with image/video counts and total sizes."""
        img_count = 0
        vid_count = 0
        img_bytes = 0
        vid_bytes = 0
        img_original_bytes = 0
        vid_original_bytes = 0
        total_original_bytes = 0
        for plan in self.plans:
            path = plan.analysis.item.path
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            original = self._original_sizes.get(path)
            total_original_bytes += original if original is not None else size
            if plan.analysis.item.kind == MediaKind.IMAGE:
                img_count += 1
                img_bytes += size
                if original is not None:
                    img_original_bytes += original
            else:
                vid_count += 1
                vid_bytes += size
                if original is not None:
                    vid_original_bytes += original
        total_count = img_count + vid_count
        total_bytes = img_bytes + vid_bytes
        text = tr(
            "Bilder: {img_count} ({img_size})  "
            "|  Videos: {vid_count} ({vid_size})  "
            "|  Gesamt: {total_count} ({total_size})"
        ).format(
            img_count=img_count,
            img_size=self._size_summary(img_bytes, img_original_bytes),
            vid_count=vid_count,
            vid_size=self._size_summary(vid_bytes, vid_original_bytes),
            total_count=total_count,
            total_size=self._size_summary(total_bytes, total_original_bytes),
        )
        self.stats_label.setText(text)

    def _size_summary(self, current_bytes: int, original_bytes: int) -> str:
        """Return a size label, adding a before/after delta when the category changed."""
        if original_bytes <= 0 or original_bytes == current_bytes:
            return human_size(current_bytes)
        return f"{human_size(original_bytes)} -> {human_size(current_bytes)}"

    def _update_missing_exif_badge(self) -> None:
        """Show the count of missing-EXIF files directly on the warning button."""
        count = len(missing_exif_rows(self.results))
        self.missing_button.set_badge_text(str(count))

    def _update_pairs_badge(self) -> None:
        """Show the count of IMG_/IMG_E duplicate pairs on the pairs button."""
        try:
            count = len(find_pairs(self.results))
        except Exception:  # noqa: BLE001
            LOGGER.debug("Pair badge update failed", exc_info=True)
            count = 0
        self.pairs_button.set_badge_text(str(count))

