"""Acceptance tests at the BOM/resolver level against the generated DEMO basket workbook.
(The bom agent owns tests/test_bom_*.py for unit coverage; these are the V1 criteria.)
AC-02 BOM loads, AC-05 known P/N -> correct BOM row, AC-06 correct designators, D7-1.
"""
from __future__ import annotations

import pytest

from asc_support import need
from asc_pipeline import BAG_A, BAG_B, BAG_B_DESIG, label_codes


def resolve(codes, bom):
    return need("polybench.assembly_scanner.resolver").resolve(list(codes), bom)


def test_ac02_bom_a_loads(bom_a):
    assert bom_a.source_path.endswith("demo_basket.xlsx")
    placeable = [ln for ln in bom_a.lines if ln.designators and ln.fitted]
    # Basket sheet: 24 part rows (rows 5..28), every one with designators.
    basket = {ln.mouser_pn for ln in bom_a.lines if ln.sheet == "Basket"}
    assert len(basket) == 24
    assert placeable


# (mouser P/N, Basket row (Excel 1-based), designators) -- the rows of the generated workbook
BASKET_SPOT = [
    ("901-DEMO-R2K2", 19, ("R4", "R5")),
    ("902-DEMO-C100N", 6, BAG_B_DESIG),
    ("903-DEMO-XCVR/MF", 14, ("IC2",)),             # '/' kept
    ("904-DEMO-ADC1", 13, ("IC1",)),
    ("905-DEMO-R1K", 22, ("R8", "R11", "R12", "R13")),
    ("906-DEMO-C1N2", 27, ("C14", "C16")),   # decided row below the original block
    ("907-DEMO-MCU1", 26, ("IC3",)),
]


@pytest.mark.parametrize("pn,row,desig", BASKET_SPOT, ids=[p for p, _, _ in BASKET_SPOT])
def test_ac05_ac06_known_pn_resolves_to_row_and_designators(bom_a, pn, row, desig):
    r = resolve(label_codes(pn, "whatever"), bom_a)
    assert r.status.value == "MATCH"
    assert r.mouser_pn == pn
    assert set(r.designators) == set(desig)
    assert len(r.designators) == len(desig), "duplicate designators"
    assert any(ln.sheet == "Basket" and ln.row == row for ln in r.lines), \
        f"expected the Basket row {row}; got {[(l.sheet, l.row) for l in r.lines]}"


@pytest.mark.parametrize("code", ["901-demo-r2k2", "  901-DEMO-R2K2 ", "901-DEMO-R2K2\n"])
def test_pn_normalisation(bom_a, code):
    assert resolve([code], bom_a).designators == ("R4", "R5")


@pytest.mark.parametrize("pn", ["930-DEMO-CL1", "920-DEMO-R560", "930-DEMO-PASTE"],
                         ids=["tuning-cap", "tuning-R6-neighbour", "consumable-paste"])
def test_extra_items_are_known_but_not_placeable(bom_a, pn):
    r = resolve(label_codes(pn, "x"), bom_a)
    assert r.status.value == "MATCH_NO_PLACEMENT" and r.designators == ()


def test_not_in_bom(bom_a):
    r = resolve(label_codes("999-DEMO-NOTINBOM", "VIB R4"), bom_a)
    assert r.status.value == "NOT_IN_BOM" and r.mouser_pn == "999-DEMO-NOTINBOM"
    assert r.designators == ()


def test_no_candidate(bom_a):
    assert resolve(["40000001", "018", "TW"], bom_a).status.value == "NO_CANDIDATE"
    assert resolve([], bom_a).status.value == "NO_CANDIDATE"


# ---- D7: the label's Cust P/N is never used -------------------------------------------------
def test_d7_truncated_cust_pn_gives_full_bom_list(bom_a):
    r = resolve(BAG_B, bom_a)
    assert set(r.designators) == set(BAG_B_DESIG) and len(r.designators) == 9


@pytest.mark.parametrize("cust", ["DEMO1 R9,10", "DEMO1 C2..", "", "R4"])
def test_d7_cust_pn_content_does_not_change_result(bom_a, cust):
    base = resolve(BAG_A, bom_a)
    r = resolve(label_codes("901-DEMO-R2K2", cust), bom_a)
    assert (r.status, r.mouser_pn, r.designators) == (base.status, base.mouser_pn, base.designators)


def test_d7_cust_pn_alone_is_no_candidate(bom_a):
    r = resolve(["DEMO1 R4,5", "PDEMO1 R4,5"], bom_a)
    assert r.status.value == "NO_CANDIDATE" and r.designators == ()
