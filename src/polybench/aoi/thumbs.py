"""Small JPEG previews of the uploaded photos (HEIC cannot be shown by a browser). Cached under the batch folder."""
from __future__ import annotations

import hashlib
from pathlib import Path


def thumbnail(src: Path, cache: Path, width: int = 360) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / f"{hashlib.sha1(str(src).encode()).hexdigest()[:12]}_{width}.jpg"
    if out.is_file():
        return out
    import pillow_heif
    from PIL import Image, ImageOps
    pillow_heif.register_heif_opener()
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((width, width * 3))
        im.save(out, "JPEG", quality=82)
    return out
