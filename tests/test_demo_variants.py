"""The ESP32-S3 dev board demo: two variants selected by the top protocol file. HOST-ONLY."""
from pathlib import Path

from polybench import protocol
import pytest

EX = Path(__file__).resolve().parents[1] / "examples" / "esp32-s3-board"


def test_yd_variant_is_the_default_with_layout_aoi_and_gpio48():
    p = protocol.load(str(EX / "yd-esp32-s3.pcbv.yaml"))
    assert not [i for i in p.issues if i.level == "error"]
    assert p.data["aoi"].get("mode") != "diff" and set(p.data["aoi"]["photos"]) == {"D01", "D02"}     # layout mode since 10 Nov 2026
    text = (EX / "function.yaml").read_text(encoding="utf-8")
    assert 'send: "led 48"' in text and "Micro-USB" not in text


def test_devkitc_variant_keeps_the_checklist_and_gpio38_default():
    p = protocol.load(str(EX / "devkitc-1.pcbv.yaml"))
    assert not p.data.get("aoi")
    assert "Micro-USB" in (EX / "function_devkitc-1.yaml").read_text(encoding="utf-8")


def test_committed_demo_photos_have_no_exif_and_are_jpeg():
    pytest.importorskip("PIL")
    from polybench.aoi import privacy
    for name in ("D01_top.jpg", "D02_top.jpg"):
        data = (EX / "aoi" / name).read_bytes()
        assert data[:2] == b"\xff\xd8" and not privacy.has_metadata(data) and b"Exif" not in data[:65536]
