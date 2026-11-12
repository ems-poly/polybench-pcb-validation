"""Shared pieces of the ported Assembly Scanner tests (the PoC's tests/conftest.py, kept as a plain module)."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"


def need(modname: str):
    """Import an app module; if it is missing, FAIL (red), do not skip."""
    try:
        return importlib.import_module(modname)
    except ModuleNotFoundError as e:
        if e.name == modname or modname.startswith(str(e.name)):
            pytest.fail(f"{modname} is missing", pytrace=False)
        raise


def load_image_bgr(path: Path, max_side: int | None = None):
    """Load a label photo (HEIC/JPG/PNG) as a BGR ndarray, EXIF-rotated, optionally downscaled."""
    import cv2
    import numpy as np
    from PIL import Image, ImageOps
    import pillow_heif
    pillow_heif.register_heif_opener()
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    a = cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)
    if max_side and max(a.shape[:2]) > max_side:
        s = max_side / max(a.shape[:2])
        a = cv2.resize(a, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    return a
