"""NConvert/pillow-heif wrappers: image conversion, orientation, depth, JPG fix."""

from __future__ import annotations

import os
from pathlib import Path

from ..core.logging_config import get_logger
from ..core.models import AppSettings
from ..core.processes import run_process

LOGGER = get_logger(__name__)


def _heic_display_dims(path: Path) -> tuple[int, int] | None:
    """Return the display-oriented pixel size of a HEIC/HEIF file via pillow-heif.

    pillow-heif (libheif) applies the stored rotation on decode, so the returned
    size is what a correct viewer shows. This is the same decode path the GUI
    preview uses, so it reflects the true orientation regardless of what NConvert
    does with the file.
    """
    try:
        import pillow_heif
        from PIL import Image

        pillow_heif.register_heif_opener()
        with Image.open(str(path)) as im:
            return int(im.width), int(im.height)
    except Exception:  # noqa: BLE001
        LOGGER.warning("Could not measure HEIC display size for orientation check: %s", path, exc_info=True)
        return None



def _jpeg_raster_dims(path: Path) -> tuple[int, int] | None:
    """Return the physical raster size of a JPEG (ignoring any orientation tag)."""
    try:
        from PIL import Image

        with Image.open(str(path)) as im:
            return int(im.width), int(im.height)
    except Exception:  # noqa: BLE001
        LOGGER.warning("Could not measure JPEG raster size for orientation check: %s", path, exc_info=True)
        return None



def _output_needs_orientation_fix(
    display_dims: tuple[int, int] | None,
    raster_dims: tuple[int, int] | None,
) -> bool:
    """Return True when the output raster is the transpose of the display size.

    That is the unambiguous signature of a 90°/270° source whose rotation the
    converter failed to apply: the pixels are still in sensor orientation. Square
    images and equal sizes never trigger, so a correctly oriented output can never
    be flagged (no false positives). 180°/mirror-only cases do not change the
    dimensions and therefore cannot be detected this way.
    """
    if display_dims is None or raster_dims is None:
        return False
    dw, dh = display_dims
    ow, oh = raster_dims
    if dw == dh:
        return False
    return (ow, oh) == (dh, dw)



def _rerender_heic_as_jpeg(source: Path, target: Path, quality: int) -> None:
    """Re-render a HEIC/HEIF source to JPEG via pillow-heif (correctly oriented)."""
    import pillow_heif
    from PIL import Image

    pillow_heif.register_heif_opener()
    with Image.open(str(source)) as im:
        im.convert("RGB").save(str(target), "JPEG", quality=quality)



def _verify_heic_output_orientation(source: Path, target: Path, quality: int) -> None:
    """Repair a HEIC->JPEG output that the converter left un-rotated.

    Compares the source's true display size (pillow-heif) against the produced
    JPEG's raster size. On a clear 90/270 transpose mismatch, the JPEG is
    re-rendered from pillow-heif so its pixels match the display orientation. The
    downstream Orientation-tag strip then remains correct.
    """
    if _output_needs_orientation_fix(_heic_display_dims(source), _jpeg_raster_dims(target)):
        LOGGER.warning(
            "Converter did not apply HEIC/HEIF orientation for %s; re-rendering output via pillow-heif.",
            source,
        )
        _rerender_heic_as_jpeg(source, target, quality)



def convert_image(source: Path, target: Path, settings: AppSettings) -> None:
    """Convert an image to JPEG using the configured XnConvert encoder.

    The EXIF orientation is baked into the pixels so the re-encoded output is in
    display orientation: JPEG sources get a lossless ``-jpegtrans exif`` pre-pass
    (nconvert does not otherwise rotate JPEG pixels), and libheif is expected to apply
    the HEIF rotation/mirror transforms when nconvert decodes HEIC/HEIF. Because that
    HEIC behavior is build-dependent, the HEIC/HEIF output is verified afterwards: if
    the produced JPEG is still in sensor (un-rotated) orientation, it is re-rendered
    from pillow-heif so the pixels match the true display orientation. PNG carries no
    EXIF orientation. The downstream metadata copy then strips the Orientation tag so
    viewers do not rotate the already-oriented pixels a second time. A 180°/mirror-only
    source (no dimension change) cannot be verified this way and still relies on the
    decoder applying the transform.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower()
    quality = settings.images.heic_heif_jpeg_quality if suffix in {".heic", ".heif"} else settings.images.jpeg_quality
    LOGGER.info(
        "Converting image with XnConvert/NConvert source=%s target=%s executable=%s quality=%s",
        source,
        target,
        settings.tools.xnconvert,
        quality,
    )
    encode_source = source
    baked: Path | None = None
    if suffix in {".jpg", ".jpeg"}:
        baked = target.parent / f"{target.stem}.baked{target.suffix}"
        LOGGER.info("Baking EXIF orientation into pixels losslessly source=%s baked=%s", source, baked)
        run_process(
            [
                settings.tools.xnconvert,
                "-quiet",
                "-overwrite",
                "-jpegtrans",
                "exif",
                "-o",
                str(baked),
                str(source),
            ]
        )
        encode_source = baked
    try:
        args = [
            settings.tools.xnconvert,
            "-overwrite",
            "-out",
            "jpeg",
            "-q",
            str(quality),
            "-o",
            str(target),
            str(encode_source),
        ]
        run_process(args)
    finally:
        if baked is not None and baked.exists():
            baked.unlink()
    if suffix in {".heic", ".heif"}:
        _verify_heic_output_orientation(source, target, quality)



def maintain_jpeg(source: Path, target: Path, settings: AppSettings) -> None:
    """Apply lossless JPEG orientation and EXIF thumbnail maintenance with NConvert."""
    target.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.info(
        "Maintaining JPEG source=%s target=%s executable=%s rotate_by_exif=%s rebuild_thumb=%s",
        source,
        target,
        settings.tools.xnconvert,
        settings.images.jpeg_rotate_by_exif,
        settings.images.jpeg_rebuild_exif_thumbnail,
    )
    args = [settings.tools.xnconvert, "-quiet", "-overwrite", "-keepfiledate"]
    if settings.images.jpeg_rotate_by_exif:
        args.extend(["-jpegtrans", "exif"])
    if settings.images.jpeg_rebuild_exif_thumbnail:
        args.append("-buildexifthumb")
    args.extend(["-o", str(target), str(source)])
    run_process(args)




def embed_gdepth(source: Path, target_jpg: Path, settings: AppSettings) -> bool:
    """Preserve a HEIC/HEIF depth map into the converted JPEG as Google GDepth XMP.

    Extracts the primary depth image from ``source`` via pillow-heif and embeds it
    into ``target_jpg`` as base64 in the ``XMP-GDepth:DepthImage`` tag (the Google
    "Lens Blur"/depth format). This keeps the depth pixels recoverable/re-editable in
    Google Photos and depth-aware editors after the HEIC has been flattened to JPEG.

    Best-effort: returns True on success, False (with a logged warning) if the source
    has no readable depth image or a step fails, so a failure never aborts the
    conversion of the primary image.
    """
    import tempfile

    try:
        import pillow_heif
    except Exception:  # pragma: no cover - dependency guaranteed at runtime
        LOGGER.warning("pillow-heif unavailable; cannot preserve depth for %s", source)
        return False

    tmp_png: Path | None = None
    try:
        heif = pillow_heif.open_heif(str(source), convert_hdr_to_8bit=False)
        depth_images = heif.info.get("depth_images") or []
        if not depth_images:
            LOGGER.info("No embedded depth image found in %s; skipping GDepth", source)
            return False
        depth = depth_images[0].to_pillow()
        fd, tmp_name = tempfile.mkstemp(suffix=".png", prefix="vmp_depth_")
        os.close(fd)
        tmp_png = Path(tmp_name)
        depth.save(str(tmp_png), "PNG")
        LOGGER.info("Embedding GDepth depth map (%sx%s) into %s", depth.width, depth.height, target_jpg)
        run_process(
            [
                settings.tools.exiftool,
                "-m",
                "-overwrite_original_in_place",
                f"-XMP-GDepth:DepthImage<={tmp_png}",
                "-XMP-GDepth:Format=RangeInverse",
                "-XMP-GDepth:Mime=image/png",
                "-XMP-GDepth:Near=0",
                "-XMP-GDepth:Far=1",
                str(target_jpg),
            ]
        )
        return True
    except Exception:
        LOGGER.warning("Failed to embed GDepth depth map for %s", source, exc_info=True)
        return False
    finally:
        if tmp_png is not None:
            try:
                tmp_png.unlink()
            except OSError:
                pass


