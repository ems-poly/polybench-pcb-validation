"""Scenario tests: simulated code streams at 30 fps with injected time, driven through
resolve -> Debouncer -> Fusion client. Each scenario runs twice:

  spy  - a recording stand-in for FusionClient (needs app.resolver, app.state, app.bom)
  mcp  - the real FusionClient against tests/fake_fusion_mcp.py, so the assertions also hold
         on the wire (needs app.fusion_client too)

Acceptance ids: see docs/ACCEPTANCE.md (AC-07..AC-11, UX-1..UX-4, D7-1).
"""
from __future__ import annotations

import pytest

from asc_support import need
from asc_fake_fusion_mcp import FakeFusionMCP
from asc_pipeline import (BAG_A, BAG_A_DESIG, BAG_B, BAG_B_DESIG, BAG_TUNING, BAG_UNKNOWN,
                            FPS, JUNK, Pipeline, SpyFusion, run_stream)


@pytest.fixture(params=["spy", "mcp"])
def rig(request, bom_a):
    """Yields (pipeline, spy, fake_or_None)."""
    if request.param == "spy":
        spy = SpyFusion()
        yield Pipeline(bom_a, spy), spy, None
    else:
        fc = need("polybench.assembly_scanner.fusion_client")
        with FakeFusionMCP() as fake:
            spy = SpyFusion(inner=fc.FusionClient(fake.url))
            yield Pipeline(bom_a, spy), spy, fake


def wire_calls(fake):
    return len(fake.tool_calls()) if fake else 0


def fusion_ops_after(pipe, t):
    return sum(k.fusion_ops for k in pipe.ticks if k.t > t)


def first_fire_time(pipe):
    return next(k.t for k in pipe.ticks if k.fired is not None)


# ---- AC-08/AC-09 + UX-4: bag A held 2 s -> exactly one highlight ------------------------
def test_bag_held_2s_highlights_once(rig):
    pipe, spy, fake = rig
    run_stream(pipe, [(BAG_A, 2.0)])
    assert spy.highlights == [BAG_A_DESIG]
    t_fire = first_fire_time(pipe)
    assert fusion_ops_after(pipe, t_fire) == 0, "Fusion touched again while the same bag stayed"
    assert len(pipe.fired) == 1
    if fake:
        writes = [c for c in fake.tool_calls() if c.mentions("R4", "R5")]
        assert writes, "no tools/call carried the designators R4, R5"


def test_single_frame_glimpse_does_not_fire(rig):
    """'same code stable for N frames / X ms' -> one frame is not enough."""
    pipe, spy, fake = rig
    run_stream(pipe, [(BAG_A, 1 / FPS), (None, 2.0)])
    assert spy.log == [] and wire_calls(fake) == 0


# ---- AC-10 + UX-3: bag removed -> highlight stays ---------------------------------------
def test_bag_removed_keeps_highlight(rig):
    pipe, spy, fake = rig
    t = run_stream(pipe, [(BAG_A, 2.0)])
    n_spy, n_wire = len(spy.log), wire_calls(fake)
    run_stream(pipe, [(None, 5.0)], t0=t)
    assert len(spy.log) == n_spy and wire_calls(fake) == n_wire, "removal touched Fusion"
    assert spy.current == BAG_A_DESIG
    if fake:
        assert fake.fusion.selected == BAG_A_DESIG


# ---- AC-11: different bag -> clear + highlight B ----------------------------------------
@pytest.mark.parametrize("gap_s", [3.0, 0.0], ids=["removed-then-B", "direct-swap"])
def test_bag_b_replaces_highlight(rig, gap_s):
    pipe, spy, fake = rig
    run_stream(pipe, [(BAG_A, 2.0), (None, gap_s), (BAG_B, 2.0)])
    assert spy.highlights == [BAG_A_DESIG, BAG_B_DESIG]
    i = spy.log.index(("highlight", BAG_B_DESIG))
    assert spy.log[i - 1] == ("clear", ()), "previous highlight not cleared before B"
    assert spy.current == BAG_B_DESIG
    if fake:
        assert any(c.mentions(*BAG_B_DESIG) for c in fake.tool_calls())
        assert fake.fusion.selected == BAG_B_DESIG


# ---- AC-12 + UX-2: unknown part -> no Fusion call, highlight intact ---------------------
@pytest.mark.parametrize("bag, status", [
    (BAG_UNKNOWN, "NOT_IN_BOM"),
    (BAG_TUNING, "MATCH_NO_PLACEMENT"),
    (JUNK, "NO_CANDIDATE"),
], ids=["not-in-bom", "tuning-kit", "no-candidate"])
def test_non_match_never_touches_fusion(rig, bag, status):
    pipe, spy, fake = rig
    t = run_stream(pipe, [(BAG_A, 2.0), (None, 1.0)])
    n_spy, n_wire = len(spy.log), wire_calls(fake)
    run_stream(pipe, [(bag, 2.0)], t0=t)
    assert len(spy.log) == n_spy, f"{status} touched the Fusion client: {spy.log[n_spy:]}"
    assert wire_calls(fake) == n_wire, f"{status} sent a tools/call"
    assert spy.current == BAG_A_DESIG
    if fake:
        assert fake.fusion.selected == BAG_A_DESIG, "board highlight disturbed"
    seen = {k.resolution.status.value for k in pipe.ticks if k.resolution and k.t >= t}
    assert seen == {status}


def test_not_in_bom_is_reported_to_the_ui_once(rig):
    """The user gets a status message (the Debouncer fires the NOT_IN_BOM resolution once)."""
    pipe, spy, fake = rig
    run_stream(pipe, [(BAG_UNKNOWN, 2.0)])
    fired = [r for r in pipe.fired if r is not None]
    assert [r.status.value for r in fired] == ["NOT_IN_BOM"]
    assert fired[0].mouser_pn == "999-DEMO-NOTINBOM"
    assert spy.log == [] and wire_calls(fake) == 0


# ---- UX-4: flicker -> no re-fire --------------------------------------------------------
@pytest.mark.parametrize("dropout", [None, JUNK], ids=["nothing-decoded", "pn-missed"])
def test_flicker_does_not_refire(rig, dropout):
    pipe, spy, fake = rig
    segs = [(BAG_A, 0.5)]
    for _ in range(10):                 # one dropped frame every 0.2 s for 2 s
        segs += [(dropout, 1 / FPS), (BAG_A, 0.2)]
    run_stream(pipe, segs)
    assert spy.highlights == [BAG_A_DESIG]
    assert fusion_ops_after(pipe, first_fire_time(pipe)) == 0


def test_same_bag_rearms_after_absence(rig):
    """Brief: 'ignore same code until it disappears'; after the re-arm gap it may fire again."""
    pipe, spy, fake = rig
    run_stream(pipe, [(BAG_A, 1.0), (None, 2.0), (BAG_A, 1.0)])
    assert spy.highlights == [BAG_A_DESIG, BAG_A_DESIG]


# ---- D7: designators come from the BOM file only, never from the label's Cust P/N -------
def test_truncated_cust_pn_still_highlights_full_bom_list(rig):
    pipe, spy, fake = rig
    run_stream(pipe, [(BAG_B, 2.0)])
    assert spy.highlights == [BAG_B_DESIG]          # 9 caps, though the label says 'DEMO1 C2..'
    if fake:
        assert fake.fusion.selected == BAG_B_DESIG


def test_misleading_cust_pn_is_ignored(rig):
    from asc_pipeline import label_codes
    pipe, spy, fake = rig
    lying = label_codes("901-DEMO-R2K2", "DEMO1 R9,10")
    run_stream(pipe, [(lying, 2.0)])
    assert spy.highlights == [BAG_A_DESIG]


def test_cust_pn_alone_never_resolves(rig):
    """Only the Cust P/N barcode in view (P/N barcode covered): no match, no Fusion call."""
    pipe, spy, fake = rig
    run_stream(pipe, [(("DEMO1 R4,5", "40000001"), 2.0)])
    assert spy.log == [] and wire_calls(fake) == 0
    assert all(k.resolution.status.value == "NO_CANDIDATE" for k in pipe.ticks if k.resolution)


# ---- frame level: the generated DEMO label image through scanner.decode ------------------------------
def test_frames_demo_label_held_then_removed(rig, demo_files):
    """Photo of bag A for 1 s, then blank frames for 2 s, at 10 fps: one highlight, kept."""
    import numpy as np
    from asc_support import load_image_bgr
    pipe, spy, fake = rig
    pipe.decode = need("polybench.assembly_scanner.scanner").decode
    label = load_image_bgr(demo_files["label_a.heic"], 1920)
    blank = np.full_like(label, 255)
    t = 0.0
    for frame, n in [(label, 10), (blank, 20)]:
        for _ in range(n):
            pipe.step_frame(frame, t)
            t += 0.1
    assert spy.highlights == [BAG_A_DESIG] and spy.current == BAG_A_DESIG
    if fake:
        assert fake.fusion.selected == BAG_A_DESIG
