"""Module C — image validation with Pillow (streamed from disk, never fully decoded into RAM
except for the optional resized upload copy)."""
from __future__ import annotations

import hashlib
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from .config import Settings

EXT = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}


class InvalidImageError(ValueError):
    pass


@dataclass
class ImageInfo:
    sha256: str
    width: int
    height: int
    format: str
    bytes: int

    @property
    def ext(self) -> str:
        return EXT.get(self.format, ".img")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_image(path: Path, settings: Settings) -> ImageInfo:
    size = path.stat().st_size
    if size < settings.min_file_bytes:
        raise InvalidImageError(f"file too small ({size} bytes)")
    if size > settings.max_download_bytes:
        raise InvalidImageError(f"file too large ({size} bytes)")
    Image.MAX_IMAGE_PIXELS = settings.max_pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as im:          # reads header only
                fmt = im.format
                width, height = im.size
                if fmt not in settings.allowed_formats:
                    raise InvalidImageError(f"unsupported format {fmt}")
                if width * height > settings.max_pixels:
                    raise InvalidImageError(f"too many pixels ({width}x{height})")
                im.verify()                        # structural check without full decode
            with Image.open(path) as im:           # verify() invalidates the object; reopen
                im.draft(None, (min(width, 256), min(height, 256)))
                im.load()                          # catches truncated data
    except InvalidImageError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
        raise InvalidImageError(f"decompression bomb suspected: {e}") from None
    except Exception as e:  # Pillow raises many types for corrupt input
        raise InvalidImageError(f"corrupt or unreadable image: {type(e).__name__}: {e}") from None
    if width < settings.min_width or height < settings.min_height:
        raise InvalidImageError(f"image too small ({width}x{height})")
    return ImageInfo(sha256=sha256_file(path), width=width, height=height, format=fmt, bytes=size)


def make_upload_copy(src: Path, dest: Path, max_edge: int) -> Path:
    """Write a copy for ChatGPT upload; downscale if larger than max_edge. Original untouched.
    Metadata (EXIF incl. GPS) is not carried over to the re-encoded copy."""
    with Image.open(src) as im:
        if max_edge and max(im.size) > max_edge:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((max_edge, max_edge))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            out = dest.with_suffix(".jpg")
            im.save(out, "JPEG", quality=90)
            return out
    dest.write_bytes(src.read_bytes())
    return dest
