"""The YD-ESP32-S3 KiCad redraw and the YD test plan agree with each other. HOST-ONLY: reads the committed files, no KiCad, no board."""
import re
from pathlib import Path

from polybench import protocol
from polybench.kicad import pcbfile
from polybench.kicad.select import plan_targets

EX = Path(__file__).resolve().parents[1] / "examples" / "esp32-s3-board"
KI = EX / "kicad" / "yd-esp32-s3"


def _plan_rows():
    rows = []
    for ln in (EX / "YD_ESP32_S3_TEST_PLAN.md").read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) >= 15 and re.fullmatch(r"[UPF]\d+", cells[0]):
            sp = lambda s: [] if s in ("—", "") else [x.strip() for x in s.split(",")]
            rows.append(dict(id=cells[0], probe=sp(cells[2]) + sp(cells[3]), nets=sp(cells[4]), highlight=sp(cells[5])))
    return rows


def test_yd_protocol_loads_clean_with_its_own_plan():
    p = protocol.load(str(EX / "yd-esp32-s3.pcbv.yaml"))
    assert not [i for i in p.issues if i.severity in ("error", "warning")]
    assert "test_plan: {file: YD_ESP32_S3_TEST_PLAN.md" in (EX / "yd-esp32-s3.pcbv.yaml").read_text(encoding="utf-8")
    assert "test_plan: {file: ESP32_S3_BOARD_TEST_PLAN.md" in (EX / "devkitc-1.pcbv.yaml").read_text(encoding="utf-8")   # the other variant is untouched


def test_every_plan_highlight_pad_and_net_exists_on_the_redrawn_board():
    snap = pcbfile.load(KI / "yd-esp32-s3.kicad_pcb")
    rows = _plan_rows()
    assert len(rows) >= 10
    for r in rows:
        hl = r["highlight"] + [x for x in r["probe"] if x not in r["highlight"]]
        plan = plan_targets(snap, [h for h in hl if "." not in h], [h for h in hl if "." in h], r["nets"])
        assert not (plan.parts_missing + plan.pins_missing + plan.nets_missing), r["id"]


def test_bom_designators_are_the_board_designators_and_the_header_pins_are_the_schematic_ones():
    import yaml
    bom = yaml.safe_load((EX / "assembly.yaml").read_text(encoding="utf-8"))["bom"]
    des = {d for line in bom for d in line["designators"]}
    snap = pcbfile.load(KI / "yd-esp32-s3.kicad_pcb")
    refs = {f.ref for f in snap.footprints}
    assert des == refs and len(refs) == 43
    plan = plan_targets(snap, [], ["J1.21", "J1.22", "J1.1", "J2.2"], ["5V_HDR", "VDD33", "U0TXD"])
    assert not (plan.pins_missing + plan.nets_missing)
    assert plan.net_pad_count["5V_HDR"] == 3      # J1.21, D3 anode, IN-OUT.1


def test_no_machine_paths_in_the_redraw_folder():
    bad = re.compile(r"/Users/|/private/tmp")
    for f in KI.rglob("*"):
        if f.is_file() and f.suffix in (".md", ".py", ".sh", ".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_sym", ".json"):
            assert not bad.search(f.read_text(errors="ignore", encoding="utf-8")), f
