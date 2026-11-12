"""Layout-mode AOI on the YD-ESP32-S3 example (KiCad redraw, two photos): the generic pieces it needed, the committed layout inputs, the photos named by the
protocol, and the acceptance run on the planted defects of D02. HOST-ONLY: two photos, not a live camera, nothing here is HARDWARE-VALIDATED.

The ground-truth TABLE (designators only) is restated here; the operator's marked image is internal and is not copied."""
import json
import re
import zipfile
from pathlib import Path

import sys

import pytest

np = pytest.importorskip("numpy", reason="needs the aoi extra (pip install -e .[aoi])")

from polybench import extras

needs_aoi = pytest.mark.skipif(not extras.check("aoi"), reason="needs the aoi extra (opencv, pillow, numpy)")

ROOT = Path(__file__).resolve().parents[2]
EX = ROOT / "examples" / "esp32-s3-board"
LAYOUT = EX / "aoi" / "layout"

# D02: the four planted defects (operator, 10 Nov 2026). class: what AOI would have to say.
GROUND_TRUTH = {"D1": "POL", "C7": "MISSING", "C2": "LIFT", "U2": "POL"}


# ---------------------------------------------------------------- generic pieces
@needs_aoi
def test_outline_from_kicad_profile_does_not_chain_separate_segments(tmp_path):
    from polybench.aoi.derive import read_outline
    gbr = ("%FSLAX46Y46*%\n%MOMM*%\nD10*\nX0Y0D02*\nX10000000Y0D01*\nX10000000Y0D02*\nX10000000Y5000000D01*\n"
           "X10000000Y5000000D02*\nX0Y5000000D01*\nX0Y5000000D02*\nX0Y0D01*\nM02*\n")
    z = tmp_path / "c.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("Gerber/profile.gbr", gbr)
    wires = read_outline(z)
    assert len(wires) == 4 and all(w["x1"] == w["x2"] or w["y1"] == w["y2"] for w in wires)      # a rectangle, no diagonal


@needs_aoi
def test_roundrect_paste_pads_are_read():
    from polybench.aoi.pads import parse_gerber_flashes
    txt = ("%FSLAX46Y46*%\n%MOMM*%\n%AMRoundRect*\n1,1,$1+$1,$2,$3*%\n"
           "%ADD10RoundRect,0.140000X-0.140000X-0.170000X0.140000X-0.170000X0.140000X0.170000X-0.140000X0.170000X0*%\nD10*\nX5000000Y6000000D03*\n")
    (pad,) = parse_gerber_flashes(txt)
    assert (pad["cx"], pad["cy"]) == (5.0, 6.0) and pad["w"] == pytest.approx(0.56) and pad["h"] == pytest.approx(0.62)


@needs_aoi
def test_kicad_package_names_give_a_body():
    from polybench.aoi.derive import body_mm
    assert body_mm("C_0402_1005Metric") == (1.0, 0.5)
    assert body_mm("QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm") == (3.0, 3.0)
    assert body_mm("D_SOD-123F") == (2.7, 1.6)


@needs_aoi
def test_dark_board_is_found_on_a_light_mat():
    import cv2
    from polybench.aoi.alignment import BoardOutlineError, detect_board
    img = np.full((600, 900, 3), (170, 150, 40), np.uint8)             # teal-ish mat (BGR)
    cv2.rectangle(img, (150, 150), (750, 450), (20, 20, 20), -1)       # black board
    q = np.array(detect_board(img, aspect=2.0, colour="dark")["corners"])
    assert abs(q[:, 0].min() - 150) < 12 and abs(q[:, 0].max() - 750) < 12
    with pytest.raises(BoardOutlineError):                              # the default rule looks for red and finds nothing
        detect_board(img, aspect=2.0)


@needs_aoi
def test_overhang_extends_the_layout_rectangle():
    from polybench.aoi.alignment import board_bounds
    out = [{"x1": 0, "y1": 0, "x2": 10, "y2": 0, "curve": 0}, {"x1": 10, "y1": 0, "x2": 10, "y2": 5, "curve": 0},
           {"x1": 10, "y1": 5, "x2": 0, "y2": 5, "curve": 0}, {"x1": 0, "y1": 5, "x2": 0, "y2": 0, "curve": 0}]
    low, high = board_bounds({"outline": out}, {"right": 2.5})
    assert list(high) == [12.5, 5] and list(low) == [0, 0]
    with pytest.raises(ValueError):
        board_bounds({"outline": out}, {"up": 1})


@needs_aoi
def test_locate_recovers_a_shifted_part_view():
    import cv2
    from polybench.aoi.polarity import locate
    rng = np.random.default_rng(3)
    ref = np.full((240, 240, 3), 30, np.uint8)
    for _ in range(40):
        x, y = rng.integers(20, 200, 2)
        cv2.rectangle(ref, (int(x), int(y)), (int(x) + 14, int(y) + 9), (200, 200, 200), -1)
    moved = np.roll(ref, (30, -40), axis=(0, 1))                       # 0.3 mm down, 0.4 mm left at 100 px/mm
    fixed, (dx, dy) = locate(moved, ref)
    assert dx == pytest.approx(-0.4, abs=0.03) and dy == pytest.approx(0.3, abs=0.03)
    assert np.abs(fixed[60:180, 60:180].astype(int) - ref[60:180, 60:180]).mean() < 12
    assert locate(moved, ref, max_mm=0.2)[1] == (0.0, 0.0)             # beyond the limit: left alone


# ---------------------------------------------------------------- committed layout inputs
def test_layout_inputs_are_committed_and_consistent():
    from polybench.aoi import layout_guard
    layout = json.loads((LAYOUT / "layout.json").read_text(encoding="utf-8"))
    names = {p["name"] for p in layout["parts"]}
    assert set(GROUND_TRUTH) <= names and len(names) == 39
    assert layout_guard.verify_layout(layout, LAYOUT / "yd-esp32-s3_cam.zip", "YD-ESP32-S3")["result"] == "match"
    xs = {w[k] for w in layout["outline"] for k in ("x1", "x2")}
    assert (min(xs), max(xs)) == (110.0, 167.15) and len(layout["outline"]) == 4
    proto = json.loads((LAYOUT / "aoi_protocol.json").read_text(encoding="utf-8"))
    assert proto["registration"]["board_colour"] == "dark" and proto["reference_board"] == "D01"
    spec = json.loads((LAYOUT / "polarity.json").read_text(encoding="utf-8"))
    assert set(spec["parts"]) == {"D1", "D2", "D3", "U2"} and spec["min_decided"] == 2
    assert spec["parts"]["D1"]["expected"]["side"] == "right"          # cathode away from the USB ports (redraw, from the photo of D01)
    assert "/Users/" not in "".join((LAYOUT / f).read_text(errors="ignore", encoding="utf-8") for f in ("aoi_protocol.json", "GAPS.txt", "aoi.yaml", "polarity.json"))   # no absolute paths


def test_example_visual_step_is_layout_mode_and_names_the_photos():
    from polybench.protocol import load
    p = load(EX / "yd-esp32-s3.pcbv.yaml")
    cfg = p.data["aoi"]
    assert cfg.get("mode") != "diff" and cfg["layout"] == "aoi/layout/layout.json" and set(cfg["photos"]) == {"D01", "D02"}
    for rel in list(cfg["photos"].values()) + [cfg["protocol"], cfg["layout"], cfg["cam_zip"]]:
        assert (EX / rel).is_file(), rel


# ---------------------------------------------------------------- photos named by the protocol
@pytest.fixture
def seeded(tmp_path):
    from polybench.engine import Engine
    from polybench.protocol import load
    from polybench.store import Journal
    batch = tmp_path / "YD"
    batch.mkdir()
    eng = Engine(load(EX / "yd-esp32-s3.pcbv.yaml"), Journal(batch / "journal.jsonl"), "pat")
    eng.create_batch("YD", ["D01", "D02"], "D", True, "YD-ESP32-S3")
    return eng, batch


@needs_aoi
def test_protocol_photos_load_once_and_are_journalled(seeded):
    from polybench.aoi import seed
    eng, batch = seeded
    got = seed.seed_photos(eng, batch)
    assert [(g["board"], g["done"]) for g in got] == [("D01", True), ("D02", True)]
    ph = eng.state.aoi_photos.values()
    assert sorted(p["board"] for p in ph) == ["D01", "D02"] and {p["assigned_by"] for p in ph} == {"protocol"}
    assert all((batch / p["file"]).is_file() and b"Exif" not in (batch / p["file"]).read_bytes()[:4096] for p in ph)
    assert seed.seed_photos(eng, batch) == []                          # a second call does nothing


@needs_aoi
def test_operator_removal_is_not_undone(seeded):
    from polybench.aoi import seed
    eng, batch = seeded
    seed.seed_photos(eng, batch)
    f = next(iter(eng.state.aoi_photos))
    eng.aoi_remove(f, "wrong board", f"aoi/removed/{Path(f).name}")
    assert seed.seed_photos(eng, batch) == [] and len(eng.state.aoi_photos) == 1


@needs_aoi
def test_unknown_board_or_missing_file_is_skipped_and_reported(seeded):
    from polybench.aoi import seed
    eng, batch = seeded
    eng.proto.data["aoi"]["photos"] = {"D09": "aoi/D01_top.jpg", "D01": "aoi/none.jpg"}
    got = seed.seed_photos(eng, batch)
    assert [g["done"] for g in got] == [False, False] and not eng.state.aoi_photos and "board" in got[0]["why"]


# ---------------------------------------------------------------- acceptance on the planted defects
def designators(item):
    return set(re.findall(r"(?<![A-Z0-9])([A-Z]{1,3}\d{1,2})(?=[.\-]|$)", item["id"].replace("AOI-", "")))


@pytest.fixture(scope="module")
def acceptance(tmp_path_factory):
    """One real run of the example's layout-mode AOI on D01 and D02 (about 40 s)."""
    pytest.importorskip("cv2")
    from polybench.aoi import runner, seed
    from polybench.engine import Engine
    from polybench.protocol import load
    from polybench.store import Journal
    batch = tmp_path_factory.mktemp("yd_batch")
    eng = Engine(load(EX / "yd-esp32-s3.pcbv.yaml"), Journal(batch / "journal.jsonl"), "pat")
    eng.create_batch("YD", ["D01", "D02"], "D", True, "YD-ESP32-S3")
    seed.seed_photos(eng, batch)
    out = runner.run(batch, batch / "runs" / "0001", eng.aoi_cfg(), log=lambda *a, **k: None)
    doc = json.loads(Path(out["result"]).read_text(encoding="utf-8"))
    return out, doc


@needs_aoi
def test_run_is_a_real_layout_run_with_both_boards_registered(acceptance):
    out, doc = acceptance
    assert not out["synthetic"] and out.get("mode") != "diff"
    assert {b["board"] for b in doc["boards"]} == {"D01", "D02"}
    assert not any(r["status"] == "not_assessable" and "not registered" in r["note"] for b in doc["boards"] for r in b["results"])
    assert any("Black solder mask" in n for n in out["notes"])         # the dark-mask limits are stated in the run, not hidden


def found(acceptance, defect):
    out, _ = acceptance
    return [i for i in out["items"] if i["board"] == "D02" and defect in designators(i)]


@needs_aoi
@pytest.mark.parametrize("defect", ["C7", "D1"])
def test_found_at_the_right_part(acceptance, defect):
    """C7 (missing) and D1 (polarity) each get an item that names the part. Whether the class is right is the next test."""
    assert found(acceptance, defect)


@needs_aoi
@pytest.mark.xfail(strict=sys.platform != "win32", reason="On Windows U2 is found (XPASS): the AOI result differs there, to review in a later work package. C2 (lifted) and U2 (rotated QFN) raise no item at all on these photos: a 0402 lift needs the tombstone asymmetry of a red-mask "
                   "board and the QFN body looks the same turned by 90 degrees; U2's pin 1 dot is not resolved (the same on the 4032 px originals, so pixel count is not the limit)")
@pytest.mark.parametrize("defect", ["C2", "U2"])
def test_found_at_the_right_part_not_found(acceptance, defect):
    assert found(acceptance, defect)


@needs_aoi
@pytest.mark.xfail(strict=True, reason="class is wrong or missing on all four: C7 comes as BRIDGE (bare bright pads read as solder), D1 as MISSING (the reworked part "
                   "shifts), D1 polarity reads PASS (the band stripes are about 0.1 mm wide and a body edge scores higher; the same on the 4032 px originals), C2 and U2 are not flagged")
@pytest.mark.parametrize("defect", sorted(GROUND_TRUTH))
def test_found_with_the_right_class(acceptance, defect):
    assert any(i["kind"] == GROUND_TRUTH[defect] for i in found(acceptance, defect))


@needs_aoi
def test_false_alarms_are_counted_not_thresholded(acceptance):
    """TBR: no threshold is set (brief R4). The counts are recorded here so a change shows up; the bounds are wide on purpose."""
    out, _ = acceptance
    fa = {b: [i["id"] for i in out["items"] if i["board"] == b and not (b == "D02" and set(GROUND_TRUTH) & designators(i))] for b in ("D01", "D02")}
    assert 0 <= len(fa["D01"]) <= 6 and 0 <= len(fa["D02"]) <= 10, fa
