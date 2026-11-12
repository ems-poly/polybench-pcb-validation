"""DIFF-06: no stored photo keeps its EXIF, GPS included. HOST-ONLY."""
import io

import pytest
pytest.importorskip("PIL")
np = pytest.importorskip("numpy")
from PIL import Image

from polybench.aoi import privacy


def jpeg_with_gps(size=(64, 48), orientation=1):
    im = Image.new("RGB", size, (90, 120, 140))
    ex = Image.Exif()
    ex[274] = orientation
    ex[271] = "TestCam"
    ex[0x8825] = {1: "N", 2: (51.0, 30.0, 0.0), 3: "W", 4: (0.0, 7.0, 0.0)}      # GPS IFD
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=ex, quality=95)
    return buf.getvalue()


def test_fixture_really_carries_gps():
    assert privacy.has_metadata(jpeg_with_gps())
    assert Image.open(io.BytesIO(jpeg_with_gps())).getexif().get_ifd(0x8825)


def test_jpeg_is_cleaned_without_reencoding_the_image_data():
    raw = jpeg_with_gps()
    clean, name, info = privacy.strip_metadata(raw, "a.jpg")
    assert not privacy.has_metadata(clean) and b"Exif" not in clean and b"TestCam" not in clean
    assert info["had_gps"] and info["stripped"] and not info["reencoded"] and name == "a.jpg"
    assert np.array_equal(np.asarray(Image.open(io.BytesIO(clean))), np.asarray(Image.open(io.BytesIO(raw))))      # same pixels


def test_rotated_jpeg_is_turned_upright_then_stripped():
    clean, _, info = privacy.strip_metadata(jpeg_with_gps((64, 48), orientation=6), "r.jpg")
    im = Image.open(io.BytesIO(clean))
    assert im.size == (48, 64) and not privacy.has_metadata(clean) and info["reencoded"]


def test_png_and_heic_leave_as_clean_lossless_png():
    pillow_heif = pytest.importorskip("pillow_heif")
    pillow_heif.register_heif_opener()
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (1, 2, 3)).save(buf, "HEIF", exif=jpeg_exif_bytes())
    clean, name, info = privacy.strip_metadata(buf.getvalue(), "IMG_1.HEIC")
    assert name == "IMG_1.png" and not privacy.has_metadata(clean) and info["had_gps"] and info["lossless"]
    assert np.array_equal(np.asarray(Image.open(io.BytesIO(clean)).convert("RGB")), np.asarray(Image.open(io.BytesIO(buf.getvalue())).convert("RGB")))
    pb = io.BytesIO()
    Image.new("RGB", (8, 8)).save(pb, "PNG")
    clean, name, _ = privacy.strip_metadata(pb.getvalue(), "x.png")
    assert name == "x.png" and clean[:4] == b"\x89PNG"


def jpeg_exif_bytes():
    ex = Image.Exif()
    ex[271] = "TestCam"
    ex[0x8825] = {1: "N", 2: (51.0, 30.0, 0.0), 3: "W", 4: (0.0, 7.0, 0.0)}
    return ex.tobytes()


def test_non_image_bytes_are_not_touched():
    out, name, info = privacy.strip_metadata(b"not an image", "x.jpg")
    assert out == b"not an image" and not info["stripped"]


def test_app_import_strips_exif_and_gps(tmp_path):
    """The route the Visual step uses (drop on a tile): the stored file has no EXIF."""
    from fastapi.testclient import TestClient
    from polybench.protocol import load
    from polybench.server import Session, create_app
    d = tmp_path / "p"
    d.mkdir()
    (d / "t.pcbv.yaml").write_text("schema: pcbv.protocol/2\ndesign: {name: T, revision: '1'}\nbatch: {default_boards: 2, label_prefix: D}\n"
                                   "aoi: {mode: diff, reference: D01}\nsteps:\n  - id: visual\n    title: Visual\n", encoding="utf-8")
    sess = Session(load(d / "t.pcbv.yaml"), tmp_path / "data", "B", True, None, None, "pat")
    c = TestClient(create_app(sess))
    r = c.post("/api/aoi/photos?name=IMG_1-D01.jpg&board=D01", content=jpeg_with_gps()).json()
    stored = (sess.dir / r["file"]).read_bytes()
    assert r["board"] == "D01" and not privacy.has_metadata(stored) and b"TestCam" not in stored
    state = c.get("/api/state").json()["aoi"]
    assert state["photos"][0]["sha256"] and len(state["photos"]) == 1
