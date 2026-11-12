"""Photo privacy: every photo that is stored by the app has its metadata removed (EXIF, including GPS, XMP, IPTC, ICC-free copy).

An iPhone photo carries the location it was taken at. The import (`/api/aoi/photos`) and the demo tooling both pass the bytes through
`strip_metadata`. JPEG is cleaned losslessly when its orientation is normal (the metadata segments are dropped, the image data is not
re-encoded). Anything else (HEIC, PNG, JPEG with a rotation tag) is decoded, rotated upright and written again as a JPEG without any
metadata: HEIC and PNG leave the import as a lossless `.png` (same decoded pixels, so AOI results do not change); a rotated JPEG as a JPEG.  HOST-ONLY.
"""
from __future__ import annotations

import io
import struct

from PIL import Image, ImageOps


def _strip_jpeg(data: bytes) -> bytes | None:
    """Drop APP1..APP15 and COM segments of a baseline/progressive JPEG; keep APP0 (JFIF), DQT, SOF, DHT, SOS and the scan.
    None when the stream is not parseable (the caller re-encodes)."""
    if data[:2] != b"\xff\xd8":
        return None
    out, i = bytearray(b"\xff\xd8"), 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker == 0xDA:                       # start of scan: the rest is entropy coded data up to EOI
            out += data[i:]
            return bytes(out)
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            out += data[i:i + 2]
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2:i + 4])
        seg = data[i:i + 2 + length]
        if not (0xE1 <= marker <= 0xEF or marker == 0xFE):
            out += seg
        i += 2 + length
    return None


def _orientation(im: Image.Image) -> int:
    try:
        return int(im.getexif().get(274, 1))
    except Exception:                            # noqa: BLE001
        return 1


def _is_image_container(data: bytes) -> bool:
    return (data[:2] == b"\xff\xd8" or data[:8] == b"\x89PNG\r\n\x1a\n" or data[4:8] == b"ftyp" or data[:4] in (b"II*\x00", b"MM\x00*")
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))


def has_metadata(data: bytes) -> bool:
    """True when the file still carries EXIF/GPS (used by the tests and the demo check)."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            ex = im.getexif()
            return bool(len(ex)) or bool(ex.get_ifd(0x8825)) or "xmp" in im.info or "exif" in im.info or "XML:com.adobe.xmp" in im.info
    except Exception:                            # noqa: BLE001
        return False


def strip_metadata(data: bytes, name: str, quality: int = 92, max_side: int | None = None) -> tuple[bytes, str, dict]:
    """(clean bytes, stored file name, info). info: {'stripped': bool, 'had_gps': bool, 'reencoded': bool, 'format': str}."""
    if not _is_image_container(data):            # no EXIF-bearing container at all (JPEG, PNG, HEIF, TIFF, WebP): nothing to strip
        return data, name, {"stripped": False, "had_gps": False, "reencoded": False, "format": "not an image container"}
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:                            # noqa: BLE001
        pass
    if data[:2] == b"\xff\xd8":                  # a JPEG PIL cannot decode (truncated) is still cleaned segment by segment
        try:
            Image.open(io.BytesIO(data)).verify()
        except Exception:                        # noqa: BLE001
            clean = _strip_jpeg(data)
            if clean is None:
                raise
            return clean, name, {"stripped": True, "had_gps": False, "reencoded": False, "format": "JPEG (undecodable)"}
    with Image.open(io.BytesIO(data)) as im:
        fmt = im.format
        had_gps = bool(im.getexif().get_ifd(0x8825))
        orient = _orientation(im)
        if fmt == "JPEG" and orient == 1 and not max_side:
            clean = _strip_jpeg(data)
            if clean is not None:
                return clean, name, {"stripped": True, "had_gps": had_gps, "reencoded": False, "format": fmt}
        img = ImageOps.exif_transpose(im).convert("RGB")
    if max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    stem = name.rsplit(".", 1)[0] if "." in name else name
    if fmt != "JPEG" and not max_side:           # HEIC / PNG: written as PNG, so the decoded pixels (and every AOI result computed from them) are identical
        img.save(buf, "PNG")
        return buf.getvalue(), stem + ".png", {"stripped": True, "had_gps": had_gps, "reencoded": True, "format": fmt, "lossless": True}
    img.save(buf, "JPEG", quality=quality, exif=b"")
    return buf.getvalue(), stem + ".jpg", {"stripped": True, "had_gps": had_gps, "reencoded": True, "format": fmt}
