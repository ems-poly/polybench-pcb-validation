"""KC-02, KC-11, KC-12: the KiCad scanner client against the Fusion client's contract, on the same small board; the existing
auto-pick rules unchanged."""
import os

import pytest

from polybench.assembly_scanner.models import HighlightResult
from polybench.kicad import settings
from polybench.kicad.client import KicadScannerClient
from polybench.kicad.fakeclient import FakeKiCadClient
from polybench.kicad.link import KiCadLink, LinkError
from polybench.kicad.model import BoardSnapshot, DocumentInfo, FootprintInfo, PadInfo

from test_fusion_targets import Fake as FusionFake        # the existing Fusion stub: IC1, IC2, R3, C2, TP1


def board(name="demo.kicad_pcb", path="/p/demo", extra=(), drop=()):
    def fp(ref, pads, x=0.0):
        f = FootprintInfo(ref, "fp-" + ref + name, x, 0.0)
        f.pads = [PadInfo(n, net, f"{ref}.{n}.{name}", x, 0, 1, 1, 0, "rect", "smd", ref) for n, net in pads]
        return f
    fps = [fp("IC1", [("1", "VIN"), ("2", "ADC_RST"), ("3", "GND")]), fp("IC2", []), fp("R3", [("1", "VIN"), ("2", "GND")]),
           fp("C2", []), fp("TP1", [("P$1", "VIN")])] + [fp(r, []) for r in extra]
    fps = [f for f in fps if f.ref not in drop]
    return BoardSnapshot(DocumentInfo(name, name[:-10], path), fps, ["VIN", "GND", "ADC_RST"])


@pytest.fixture(params=["fusion", "kicad"])
def impl(request):
    if request.param == "fusion":
        return request.param, FusionFake()
    fake = FakeKiCadClient(board())
    return request.param, KicadScannerClient(link=KiCadLink(client_factory=lambda a: fake, fake=True))


def test_parts_only(impl):
    _n, c = impl
    r = c.highlight_targets(["R3", "TP1", "Q9"])
    assert isinstance(r, HighlightResult) and r.ok and r.highlighted == ("R3", "TP1") and r.not_on_board == ("Q9",) and r.net_mode == ""


def test_mixed_lists(impl):
    _n, c = impl
    r = c.highlight_targets(["IC1"], ["IC1.2", "IC1.99"], ["VIN", "NOPE"])
    assert r.ok and "IC1" in r.highlighted and r.pins == ("IC1.2",) and r.pins_missing == ("IC1.99",)
    assert r.nets == ("VIN",) and r.nets_missing == ("NOPE",) and r.net_mode in ("nets", "pads")


def test_highlight_and_clear_shapes(impl):
    _n, c = impl
    h = c.highlight(["R3", "ZZ9"])
    assert h.ok and h.highlighted == ("R3",) and h.not_on_board == ("ZZ9",)
    assert c.clear().ok
    assert c.mode == "auto" and c.target is None


def test_interface_members_exist():
    c = KicadScannerClient(link=KiCadLink(client_factory=lambda a: FakeKiCadClient(board()), fake=True))
    for m in ("status", "list_boards", "select_board", "auto_select_for", "board_designators", "highlight", "highlight_targets",
              "clear", "footprints", "mode", "target", "last_info", "last_latency_ms"):
        assert hasattr(c, m), m
    st = c.status()
    assert {"connected", "doc_name", "is_electronics", "mode", "board", "boards", "open_boards", "active_doc", "error"} <= set(st)
    assert st["connected"] and st["doc_name"] == "demo.kicad_pcb" and st["boards"] == 1
    b = c.list_boards()[0]
    assert {"key", "name", "editor", "designators", "is_active", "is_modified"} <= set(b) and b["is_modified"] is None      # KC-11, TBR-9


# ---- board choice with several KiCad instances (KC-11, KC-12): one socket per instance
@pytest.fixture
def two(monkeypatch, sockdir):
    for n in ("api.sock", "api-4242.sock"):
        open(os.path.join(sockdir, n), "w").close()
    monkeypatch.setattr(settings, "socket_dirs", lambda: [sockdir])
    monkeypatch.delenv("KICAD_API_SOCKET", raising=False)
    monkeypatch.delenv("POLYBENCH_KICAD_SOCKET", raising=False)
    fakes = {os.path.join(sockdir, "api.sock"): FakeKiCadClient(board("main.kicad_pcb", "/p/main")),
             os.path.join(sockdir, "api-4242.sock"): FakeKiCadClient(board("other.kicad_pcb", "/p/other", extra=[f"X{i}" for i in range(30)], drop=("C2",)))}
    link = KiCadLink(client_factory=lambda a: fakes[a[len("ipc://"):]])
    return KicadScannerClient(link=link), fakes


BOM = ["IC1", "IC2", "R3", "C2", "TP1"]


def test_two_sockets_two_entries(two):
    c, _ = two
    boards = c.list_boards()
    assert [b["name"] for b in boards] == ["main.kicad_pcb", "other.kicad_pcb"] and all(b["socket"] for b in boards)


def test_auto_pick_clear_winner(two):
    c, _ = two
    best, cov, runner = c.auto_select_for(BOM)
    assert best["name"] == "main.kicad_pcb" and cov == 1.0 and best["confident"] and runner["name"] == "other.kicad_pcb"
    assert c.target["name"] == "main.kicad_pcb"


def test_auto_pick_ambiguous_tie_is_not_confident(two):
    c, _ = two
    best, cov, runner = c.auto_select_for(["IC1", "R3"])       # both boards hold them: margin 0
    assert not best["confident"] and "margin" in best["reason"] and c.target is None


def test_low_coverage_not_confident(two):
    c, _ = two
    best, cov, _r = c.auto_select_for(BOM + ["Q1", "Q2", "Q3", "Q4", "Q5"])
    assert cov < 0.95 and not best["confident"] and "coverage" in best["reason"]


def test_manual_pin_wins_and_closed_target_is_an_error_not_a_swap(two):
    c, fakes = two
    c.auto_select_for(BOM)
    pinned = c.select_board("other.kicad_pcb")
    assert c.mode == "manual" and pinned["name"] == "other.kicad_pcb"
    c.auto_select_for(BOM)
    assert c.target["name"] == "other.kicad_pcb"                          # a manual pin is never overridden
    os.remove(os.path.join(os.path.dirname(pinned["socket"][len("ipc://"):]), "api-4242.sock"))     # that KiCad closes
    st = c.status()
    assert st["connected"] and "no longer open" in st["error"] and st["board"] is None
    r = c.highlight(["R3"])
    assert not r.ok or r.highlighted == ()                                 # never the other board
    assert fakes[next(k for k in fakes if k.endswith("api.sock"))].selection == []
    assert c.select_board(None) is None and c.mode == "auto"


def test_select_board_unknown_names_the_open_ones(two):
    c, _ = two
    with pytest.raises(LinkError) as e:
        c.select_board("nope")
    assert "main.kicad_pcb" in str(e.value)


def test_highlight_goes_to_the_pinned_instance(two):
    c, fakes = two
    c.select_board("other.kicad_pcb")
    assert c.highlight(["R3"]).ok
    other = fakes[next(k for k in fakes if "4242" in k)]
    main_fake = fakes[next(k for k in fakes if k.endswith("api.sock"))]
    assert other.selection and main_fake.selection == []
