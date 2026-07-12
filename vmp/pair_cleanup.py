"""Detect iPhone ``IMG_xxxx`` / ``IMG_Exxxx`` duplicate pairs and confirm containment.

On iPhone imports the same shot often arrives twice: the original ``IMG_1234`` plus an
edited ``IMG_E1234`` with the *same* capture time. Two kinds occur:

* **crop** — the ``_E`` version has fewer megapixels (usually a 9:16 crop, same height,
  narrower width) and is fully contained inside the original. Deleting it is lossless.
* **look** — same dimensions, but the ``_E`` is a portrait/blur re-render (iPhone Portrait
  mode). It is *not* a crop, so it is surfaced for manual review, never auto-selected.

The detection (:func:`find_pairs`) is pure/metadata-only and Qt-free (mirrors
``gui/trip_selection.py``). The optional pixel confirmation (:func:`confirm_contained`)
decodes both images and runs a normalized cross-correlation template match to prove the
smaller image really lives inside the larger one before anything is deleted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .core.models import IMAGE_EXTENSIONS, AnalysisResult, MediaKind

# ``IMG_1234`` (original) and ``IMG_E1234`` (edited) share the numeric id.
_RE_BASE = re.compile(r"^IMG_(\d+)$", re.IGNORECASE)
_RE_EDIT = re.compile(r"^IMG_E(\d+)$", re.IGNORECASE)


def _suffix_family(path: Path) -> str:
    """Return the normalized suffix family (``.jpeg`` counts as ``.jpg``)."""
    suffix = path.suffix.lower()
    return ".jpg" if suffix == ".jpeg" else suffix

# Above this normalized-cross-correlation score the smaller image is considered
# to be contained (a crop) of the larger. True crop pairs empirically score
# >= 0.98; 0.9 keeps headroom for resampling/JPEG artefacts without admitting
# genuinely different renders (portrait/blur pairs score far lower).
CONTAINMENT_NCC_THRESHOLD = 0.9


@dataclass(slots=True)
class PairCandidate:
    """One detected ``IMG_`` / ``IMG_E`` pair, ready for the cleanup overlay."""

    base: AnalysisResult
    edit: AnalysisResult
    kind: str  # "crop" | "look"
    smaller_path: Path
    bigger_path: Path
    contained: bool | None = None
    ncc: float | None = None

    @property
    def is_crop(self) -> bool:
        """Return True when the edit is a smaller-resolution crop of the original."""
        return self.kind == "crop"


def _megapixels(result: AnalysisResult) -> int | None:
    """Return width*height when both dimensions are known, else None."""
    if result.width and result.height:
        return int(result.width) * int(result.height)
    return None


def _same_capture_time(a: AnalysisResult, b: AnalysisResult) -> bool:
    """Return True when both results resolve to the same capture instant.

    Pairs are always shot at the same moment, so this guards against numeric-id
    collisions between unrelated files. Falls back to date-equality when only a
    date is known, and accepts the pair if neither side has a usable timestamp
    (the filename id match then carries the decision).
    """
    ra, rb = a.resolved, b.resolved
    if ra.local_dt is None and rb.local_dt is None:
        return True
    if ra.local_dt is None or rb.local_dt is None:
        # One has a time, the other has none at all — cannot confirm the pair.
        return False
    if ra.local_date_only or rb.local_date_only:
        # A date-only side resolves to midnight; comparing exact datetimes would
        # always fail, so fall back to date equality as documented.
        return ra.local_dt.date() == rb.local_dt.date()
    return ra.local_dt == rb.local_dt


def find_pairs(results: list[AnalysisResult]) -> list[PairCandidate]:
    """Find all ``IMG_`` / ``IMG_E`` pairs among analysis results.

    Pairs are formed per directory by numeric id, require the same image suffix
    family and matching capture time, and are classified crop vs. look purely from
    dimensions. No pixels are read here.
    """
    # Index originals and edits per (parent dir, id, suffix family). Including
    # the suffix family in the key keeps IMG_1234.JPG and IMG_1234.HEIC in the
    # same directory from overwriting each other, and pairs .jpg with .jpeg.
    bases: dict[tuple[Path, str, str], AnalysisResult] = {}
    edits: dict[tuple[Path, str, str], AnalysisResult] = {}
    for result in results:
        if result.item.kind != MediaKind.IMAGE:
            continue
        path = result.item.path
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        stem = path.stem
        parent = path.parent
        family = _suffix_family(path)
        m_base = _RE_BASE.match(stem)
        if m_base:
            bases[(parent, m_base.group(1), family)] = result
            continue
        m_edit = _RE_EDIT.match(stem)
        if m_edit:
            edits[(parent, m_edit.group(1), family)] = result

    pairs: list[PairCandidate] = []
    for key, edit in edits.items():
        base = bases.get(key)
        if base is None:
            continue
        if not _same_capture_time(base, edit):
            continue

        base_mp = _megapixels(base)
        edit_mp = _megapixels(edit)
        if base_mp is not None and edit_mp is not None and edit_mp != base_mp:
            # Different resolution -> crop. The smaller one is the redundant crop.
            if edit_mp < base_mp:
                smaller, bigger, kind = edit, base, "crop"
            else:
                smaller, bigger, kind = base, edit, "crop"
        else:
            # Same (or unknown) dimensions -> portrait/blur look edit, not a crop.
            smaller, bigger, kind = edit, base, "look"

        pairs.append(
            PairCandidate(
                base=base,
                edit=edit,
                kind=kind,
                smaller_path=smaller.item.path,
                bigger_path=bigger.item.path,
            )
        )

    pairs.sort(key=lambda p: (str(p.base.item.path.parent), p.base.item.path.name))
    return pairs


def _load_gray_array_with_size(path: Path, long_edge: int = 320):
    """Load an oriented, downscaled grayscale array plus the original (w, h)."""
    import numpy as np
    from PIL import Image, ImageOps

    suffix = path.suffix.lower()
    if suffix in {".heic", ".heif"}:
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except Exception:
            return None, None
    try:
        img = Image.open(str(path))
        img = ImageOps.exif_transpose(img)
        img = img.convert("L")
    except Exception:
        return None, None
    w, h = img.size
    scale = long_edge / max(w, h)
    if scale < 1:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.BILINEAR)
    return np.asarray(img, dtype=np.float64), (w, h)


def _ncc_max(big, small) -> float:
    """Return the maximum zero-mean normalized cross-correlation of ``small`` in ``big``.

    Equivalent to OpenCV ``matchTemplate``/``TM_CCOEFF_NORMED`` best score, implemented
    with numpy only. The template (smaller image) is slid over every valid offset; each
    window is zero-meaned and normalized. Range is roughly [-1, 1]; ~1 means the template
    is present at that location.
    """
    import numpy as np

    H, W = big.shape
    h, w = small.shape
    if h > H or w > W:
        return -1.0
    t = small - small.mean()
    t_norm = float(np.sqrt((t * t).sum()))
    if t_norm == 0:
        return -1.0
    best = -1.0
    for y in range(H - h + 1):
        for x in range(W - w + 1):
            win = big[y : y + h, x : x + w]
            win = win - win.mean()
            denom = float(np.sqrt((win * win).sum())) * t_norm
            if denom == 0:
                continue
            val = float((win * t).sum() / denom)
            if val > best:
                best = val
        if best >= 0.999:
            break
    return best


_DEFAULT_TEMPLATE_SCALES: tuple[float, ...] = (1.0, 0.98, 1.02, 0.9, 1.1, 0.8)


def _template_scales(
    big_shape: tuple[int, int],
    small_shape: tuple[int, int],
    big_dims: tuple[int, int] | None,
    small_dims: tuple[int, int] | None,
) -> tuple[float, ...]:
    """Return template scale candidates, derived from original sizes when known.

    Both grayscale arrays are downscaled *independently* to the same long edge,
    so a crop whose long edge differs from the original's (e.g. a 9:16 crop of a
    landscape photo) needs a corrective factor of
    ``(big_array_scale / small_array_scale)`` before the template can match.
    Without the original dimensions a fixed fallback sweep is used, which only
    covers crops that kept the long edge.
    """
    if not big_dims or not small_dims:
        return _DEFAULT_TEMPLATE_SCALES
    big_long = max(big_dims)
    small_long = max(small_dims)
    if big_long <= 0 or small_long <= 0:
        return _DEFAULT_TEMPLATE_SCALES
    big_array_scale = max(big_shape) / big_long
    small_array_scale = max(small_shape) / small_long
    if small_array_scale <= 0:
        return _DEFAULT_TEMPLATE_SCALES
    factor = big_array_scale / small_array_scale
    derived = (factor, factor * 0.98, factor * 1.02, factor * 0.95, factor * 1.05)
    extras = tuple(s for s in _DEFAULT_TEMPLATE_SCALES if all(abs(s - d) > 1e-3 for d in derived))
    return derived + extras


def confirm_contained_from_arrays(
    big,
    small,
    big_dims: tuple[int, int] | None = None,
    small_dims: tuple[int, int] | None = None,
) -> tuple[bool, float]:
    """Confirm ``small`` (grayscale array) is contained in ``big`` (grayscale array).

    Runs a normalized cross-correlation template match across a few relative scales
    (the edit may be a same-resolution crop or slightly downscaled). When the images'
    original pixel dimensions are passed, the correct relative scale is derived from
    them, which is required for crops that changed the long edge (landscape source,
    portrait crop). The NCC math is cheap; the expensive part is decoding the source
    images, so callers should pass already-decoded arrays (e.g. reused from the
    preview thumbnails) rather than re-reading the files. Returns ``(contained, best_ncc)``.
    """
    import numpy as np
    from PIL import Image

    if big is None or small is None:
        return False, -1.0
    big = np.asarray(big, dtype=np.float64)
    small = np.asarray(small, dtype=np.float64)

    best = -1.0
    for scale in _template_scales(big.shape, small.shape, big_dims, small_dims):
        sh, sw = int(small.shape[0] * scale), int(small.shape[1] * scale)
        if sh < 8 or sw < 8 or sh > big.shape[0] or sw > big.shape[1]:
            continue
        if abs(scale - 1.0) < 1e-9:
            tmpl = small
        else:
            tmpl = np.asarray(
                Image.fromarray(small.astype(np.uint8)).resize((sw, sh), Image.BILINEAR),
                dtype=np.float64,
            )
        score = _ncc_max(big, tmpl)
        if score > best:
            best = score
        if best >= CONTAINMENT_NCC_THRESHOLD:
            break
    return best >= CONTAINMENT_NCC_THRESHOLD, best


def confirm_contained(bigger_path: Path, smaller_path: Path) -> tuple[bool, float]:
    """Confirm the smaller image is contained (a crop) of the bigger one.

    Convenience wrapper that decodes both images from disk and delegates to
    :func:`confirm_contained_from_arrays`. On decode failure returns ``(False, -1.0)``.
    """
    big, big_dims = _load_gray_array_with_size(bigger_path)
    small, small_dims = _load_gray_array_with_size(smaller_path)
    if big is None or small is None:
        return False, -1.0
    return confirm_contained_from_arrays(big, small, big_dims, small_dims)
