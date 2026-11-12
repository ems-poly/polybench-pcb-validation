"""Polarity card: rotation and pad mapping (0/90/180/270 deg, bottom side), the confirmed / amber decision, the recorded demo reads."""
import json
import math
from pathlib import Path

import pytest

from polybench import footprints as fpm
from polybench import polarity_card as pc

REC = Path(fpm.__file__).parent / "assembly_scanner" / "sim_demo_reads.json"


def synth_read(element, contacts, smds=(), wires=(), circles=()):
    """A read() over one synthetic element: a diode with pad 1 at (-1,0) and pad 2 at (+1,0) unless told otherwise."""
    def read(entity, obj=None):
        flt = {f["property"]: f["value"] for f in (obj or {}).get("filters", [])}
        if entity == "electronics.Element":
            return {"items": [dict(element, package_object_id=7)] if flt.get("name") == element["name"] else []}
        if entity == "electronics.Package":
            return {"items": [{"name": "SYN", "library": "L"}]}
        if entity == "electronics.Contact":
            return {"items": contacts}
        if entity == "electronics.Smd":
            return {"items": list(smds), "pagination": {"hasMore": False}}
        if entity == "electronics.Wire":
            return {"items": list(wires)}
        if entity == "electronics.Circle":
            return {"items": list(circles)}
        raise KeyError(entity)
    return read


CONTACTS = [{"name": "1", "x": -1.0, "y": 0.0}, {"name": "2", "x": 1.0, "y": 0.0}]


def smds_for(el, nets=("VIN", "GND")):
    out = []
    for c, net in zip(CONTACTS, nets):
        x, y = fpm.place((c["x"], c["y"]), el)
        out.append({"name": c["name"], "x": x, "y": y, "angle": el["angle"], "dx": 0.8, "dy": 0.8, "signal": net})
    return out


@pytest.mark.parametrize("angle,expect1", [(0, (-1, 0)), (90, (0, -1)), (180, (1, 0)), (270, (0, 1))])
def test_pad_rotation_counter_clockwise(angle, expect1):
    el = {"x": 10.0, "y": 5.0, "angle": angle, "mirror": 0}
    x, y = fpm.place((-1.0, 0.0), el)
    assert (round(x - 10.0, 6), round(y - 5.0, 6)) == (expect1[0], expect1[1])


def test_bottom_side_flips_x_before_rotation():
    top = fpm.place((-1.0, 0.0), {"x": 0, "y": 0, "angle": 0, "mirror": 0})
    bot = fpm.place((-1.0, 0.0), {"x": 0, "y": 0, "angle": 0, "mirror": 1})
    assert top == (-1.0, 0.0) and bot == (1.0, 0.0)
    r = fpm.place((-1.0, 0.0), {"x": 0, "y": 0, "angle": 90, "mirror": 1})      # flipped to +x, then turned to +y
    assert (round(r[0], 6), round(r[1], 6)) == (0.0, 1.0)


LINE = {"line": 8, "polarity": "Cathode band ...", "designators": ["D2"],
        "polmark": {"symbol": "tvs", "family": "sod323", "marking": "band", "terminal": "cathode", "net": "VIN", "ref": "BOM note"}}


@pytest.mark.parametrize("angle", [0, 90, 180, 270, 45])
@pytest.mark.parametrize("mirror", [0, 1])
def test_confirmed_card_follows_rotation_and_side(angle, mirror):
    el = {"name": "D2", "x": 10.0, "y": 5.0, "angle": angle, "mirror": mirror}
    fp = fpm.read_footprints(synth_read(el, CONTACTS, smds_for(el)), ["D2"])["D2"]
    assert fp["element"]["side"] == ("bottom" if mirror else "top")
    card = pc.build_card(LINE, "D2", ["D2"], fp, doc="B")
    assert card["status"] == "confirmed"
    pad = next(p for p in card["board"]["pads"] if p["pol"])
    assert pad["name"] == "1"                                            # the net decides, not the position
    cx = sum(q[0] for q in pad["poly"]) / 4
    cy = sum(q[1] for q in pad["poly"]) / 4
    want = fpm.place((-1.0, 0.0), el)
    assert math.hypot(cx - want[0], cy - want[1]) < 1e-6
    assert [p["name"] for p in card["symbol"]["pins"]] == ["K", "A"]
    assert "pad 1 (VIN)" in card["text"]


@pytest.mark.parametrize("angle,word", [(0, "left"), (90, "bottom"), (180, "right"), (270, "top"), (45, "lower-left")])
def test_text_names_the_board_side(angle, word):
    el = {"name": "D2", "x": 10.0, "y": 5.0, "angle": angle, "mirror": 0}
    fp = fpm.read_footprints(synth_read(el, CONTACTS, smds_for(el)), ["D2"])["D2"]
    assert f", {word} end on the board" in pc.build_card(LINE, "D2", ["D2"], fp)["text"]


def test_polarity_follows_the_net_when_the_pads_are_swapped():
    el = {"name": "D2", "x": 0, "y": 0, "angle": 0, "mirror": 0}
    fp = fpm.read_footprints(synth_read(el, CONTACTS, smds_for(el, nets=("GND", "VIN"))), ["D2"])["D2"]
    card = pc.build_card(LINE, "D2", ["D2"], fp)
    assert card["status"] == "confirmed" and card["board"]["pol_pad"] == "2" and "right end" in card["text"]


def amber(card, reason_part):
    assert card["status"] == "amber" and card["board"] is None and card["amber"] == pc.AMBER_TEXT
    assert reason_part in card["reason"], card["reason"]
    assert card["symbol"]["kind"] == "tvs"          # the symbol and the generic marking stay


def test_amber_without_a_footprint():
    amber(pc.build_card(LINE, "D2", ["D2"], None, error="Fusion is not reachable"), "not reachable")
    amber(pc.build_card(LINE, "D2", ["D2"], {"error": "D2 is not on the open board"}), "not on the open board")
    c = pc.build_card(LINE, "D2", ["D2"], fpm.read_footprints(synth_read({"name": "D9", "x": 0, "y": 0, "angle": 0}, CONTACTS), ["D2"])["D2"])
    amber(c, "not on the open board")


def test_amber_when_the_pads_cannot_be_checked_against_fusion():
    el = {"name": "D2", "x": 0, "y": 0, "angle": 0, "mirror": 0}
    amber(pc.build_card(LINE, "D2", ["D2"], fpm.read_footprints(synth_read(el, CONTACTS, []), ["D2"])["D2"]), "not proven")
    wrong = [dict(s, x=s["x"] + 0.5) for s in smds_for(el)]               # Fusion's pad is where our rotation says it is not
    amber(pc.build_card(LINE, "D2", ["D2"], fpm.read_footprints(synth_read(el, CONTACTS, wrong), ["D2"])["D2"]), "not proven")


def test_amber_when_the_net_is_missing_or_ambiguous():
    el = {"name": "D2", "x": 0, "y": 0, "angle": 0, "mirror": 0}
    for nets, part in ((("GND", "GND"), "0 pads on net VIN"), (("VIN", "VIN"), "2 pads on net VIN")):
        fp = fpm.read_footprints(synth_read(el, CONTACTS, smds_for(el, nets=nets)), ["D2"])["D2"]
        amber(pc.build_card(LINE, "D2", ["D2"], fp), part)


def test_amber_when_the_line_has_no_structured_marking_or_names_nothing():
    el = {"name": "D2", "x": 0, "y": 0, "angle": 0, "mirror": 0}
    fp = fpm.read_footprints(synth_read(el, CONTACTS, smds_for(el)), ["D2"])["D2"]
    c = pc.build_card({"line": 8, "polarity": "Cathode band"}, "D2", ["D2"], fp)
    assert c["status"] == "amber" and "no structured" in c["reason"]
    noname = dict(LINE, polmark={k: v for k, v in LINE["polmark"].items() if k != "net"})
    assert "names no pad" in pc.build_card(noname, "D2", ["D2"], fp)["reason"]


def test_amber_when_the_silkscreen_mark_disagrees():
    el = {"name": "D2", "x": 0, "y": 0, "angle": 0, "mirror": 0}
    dot_by_pad2 = [{"x": 1.6, "y": 0.5, "radius": 0.1, "layer": 21, "width": 0.2}]
    fp = fpm.read_footprints(synth_read(el, CONTACTS, smds_for(el), circles=dot_by_pad2), ["D2"])["D2"]
    amber(pc.build_card(LINE, "D2", ["D2"], fp), "beside pad 2, not pad 1")
    dot_by_pad1 = [{"x": -1.6, "y": 0.5, "radius": 0.1, "layer": 21, "width": 0.2}]
    fp = fpm.read_footprints(synth_read(el, CONTACTS, smds_for(el), circles=dot_by_pad1), ["D2"])["D2"]
    assert pc.build_card(LINE, "D2", ["D2"], fp)["status"] == "confirmed"


def test_nets_expectation_ties_pad_numbers_to_the_datasheet():
    el = {"name": "D2", "x": 0, "y": 0, "angle": 0, "mirror": 0}
    fp = fpm.read_footprints(synth_read(el, CONTACTS, smds_for(el)), ["D2"])["D2"]
    line = dict(LINE, polmark=dict(LINE["polmark"], nets={"2": "GND"}))
    assert pc.build_card(line, "D2", ["D2"], fp)["status"] == "confirmed"
    line = dict(LINE, polmark=dict(LINE["polmark"], nets={"2": "5V"}))
    assert "datasheet pin map" in pc.build_card(line, "D2", ["D2"], fp)["reason"]


# ---- the recorded demo board reads (a sanitised Fusion session): the part types of a typical BOM
@pytest.fixture(scope="module")
def demo_fp():
    rec = json.loads(REC.read_text(encoding="utf-8"))

    def read(entity, obj=None):
        a = {"entity_type": entity}
        if obj:
            a["object"] = obj
        return rec[json.dumps(a, sort_keys=True)]
    return fpm.read_footprints(read, ["IC1", "IC2", "IC3", "IC4", "Y1", "D1", "D2"])


DEMO_LINES = {
    "D2": {"line": 8, "polarity": "x", "polmark": {"symbol": "tvs", "family": "sod323", "marking": "band", "terminal": "cathode", "net": "VIN", "ref": "r"}},
    "D1": {"line": 7, "polarity": "x", "polmark": {"symbol": "led_rgb_ca", "family": "led4", "marking": "triangle", "terminal": "pad1", "pad": "1",
                                                   "nets": {"2": "LED_G", "3": "LED_B", "4": "LED_R"}, "ref": "r"}},
    "IC1": {"line": 2, "polarity": "x", "polmark": {"symbol": "ic", "family": "qfn", "marking": "pin1_dot", "terminal": "pin1", "pad": "1", "ref": "r"}},
}


def test_recorded_demo_pads_are_all_verified_against_fusion(demo_fp):
    for d, fp in demo_fp.items():
        assert not fp.get("error") and all(p["verified"] for p in fp["pads"]), d


@pytest.mark.parametrize("d,pad,where", [("D2", "1", "lower-left end"), ("D1", "1", "upper-left corner"), ("IC1", "1", "lower-right corner")])
def test_recorded_demo_parts(demo_fp, d, pad, where):
    c = pc.build_card(DEMO_LINES[d], d, [d], demo_fp[d], doc="DEMO-1")
    assert c["status"] == "confirmed", c.get("reason")
    assert c["board"]["pol_pad"] == pad and where in c["text"], c["text"]
    assert "DEMO-1" in c["source"]


def test_recorded_all_demo_ics_number_counter_clockwise(demo_fp):
    for d in ("IC1", "IC2", "IC3", "IC4"):
        assert pc.numbered_ccw(demo_fp[d]), d


# ---- the endpoint, the batch cache and the ECAD-off path (simulated ECAD replaying the recorded demo read)
@pytest.fixture
def poly_client(tmp_path, monkeypatch):
    import shutil
    from fastapi.testclient import TestClient
    from polybench.protocol import load
    from polybench.server import Session, create_app
    from helpers import DEMO
    proto = tmp_path / "proto"
    shutil.copytree(DEMO, proto)
    a = (proto / "assembly.yaml").read_text(encoding="utf-8")
    a = a.replace('polarity: "cathode band toward J1", dnf: false,',
                  'polarity: "cathode band toward J1", polmark: {symbol: diode, family: sma, marking: band, terminal: cathode, pad: "1", ref: "demo"}, dnf: false,')
    (proto / "assembly.yaml").write_text(a, encoding="utf-8")
    monkeypatch.setenv("ASCAN_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("POLYBENCH_FUSION_URL", raising=False)
    sess = Session(load(proto / "demo.pcbv.yaml"), tmp_path / "data", "pol", True, 3, None)
    with TestClient(create_app(sess)) as c:
        yield c, sess


def test_endpoint_confirmed_amber_and_none(poly_client):
    c, sess = poly_client
    d1 = c.get("/api/polarity?line=6").json()
    assert d1["status"] == "confirmed" and d1["designator"] == "D1" and d1["board"]["pol_pad"] == "1" and "pads: Fusion" in d1["source"]
    assert (sess.dir / "polarity_cache.json").exists()                  # the read is cached per document in the batch folder
    u1 = c.get("/api/polarity?line=1").json()
    assert u1["status"] == "amber" and "no structured" in u1["reason"] and u1["board"] is None
    assert c.get("/api/polarity?line=4").json()["status"] == "none"       # decoupling caps carry no polarity
    assert c.get("/api/polarity?line=999").status_code == 404


def test_part_the_simulated_ecad_does_not_know_is_amber(poly_client, tmp_path, monkeypatch):
    import polybench.assembly_scanner.sim as sim
    c, sess = poly_client
    rec = json.loads(sim.RECORDING.read_text(encoding="utf-8"))
    short = tmp_path / "short.json"
    short.write_text(json.dumps({k: v for k, v in rec.items() if '"value": "D1"' not in k}), encoding="utf-8")
    monkeypatch.setattr(sim, "RECORDING", short)
    card = c.get("/api/polarity?line=6").json()
    assert card["status"] == "amber" and "not on the open board" in card["reason"] and card["board"] is None
    assert card["amber"] == pc.AMBER_TEXT and card["symbol"]["kind"] == "diode"


def test_ecad_off_uses_the_cached_read_or_shows_amber(poly_client):
    c, sess = poly_client
    assert c.get("/api/polarity?line=6").json()["status"] == "confirmed"
    sess._pol_memo.clear()
    sess.engine.state.project["skipped"] = True                         # step 1: continue without ECAD
    cached = c.get("/api/polarity?line=6").json()
    assert cached["status"] == "confirmed" and "[cached read]" in cached["source"]
    (sess.dir / "polarity_cache.json").unlink()
    off = c.get("/api/polarity?line=6").json()
    assert off["status"] == "amber" and "ECAD is off" in off["reason"] and off["board"] is None
