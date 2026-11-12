"""BOM import + resolver tests on the generated DEMO workbooks (tests/assembly_scanner/demo_fixtures.py)."""
import pytest

from polybench.assembly_scanner.bom import load_bom, load_report, parse_designators
from polybench.assembly_scanner.models import Status
from polybench.assembly_scanner.resolver import resolve

# Every code decoded from the generated DEMO label (label_a.heic)
LABEL = ['[)>\x1e06\x1dK40000001\x1d14K018\x1dPDEMO1 R4,5\x1dQ30\x1d11K090000001\x1d4LTW\x1d1VDEMO Corp\x1e\x04',
         '40000001', '018', '901-DEMO-R2K2', 'DEMO1 R4,5', '30', 'TW', '090000001', '57DEMO']


@pytest.fixture(scope="module")
def basket(bom_a):
    return bom_a


@pytest.fixture(scope="module")
def order_bom(bom_b):
    return bom_b


# ---------------------------------------------------------------- parsing
def test_parse_designators():
    assert parse_designators("C2,C3, C4") == (("C2", "C3", "C4"), [])
    assert parse_designators("C5, C22") == (("C5", "C22"), [])
    assert parse_designators("R1-R3") == (("R1", "R2", "R3"), [])
    assert parse_designators(None) == ((), [])
    assert parse_designators("C2, see notes")[1] == ["SEE", "NOTES"]


# ---------------------------------------------------------------- basket load
def test_basket_index_shape(basket):
    rep = load_report(basket)
    roles = {s.sheet: s.role for s in rep.sheets}
    assert roles == {"Basket": "placement", "Tuning kit": "extras",
                     "Consumables": "extras", "Parts mapping": "reference"}
    assert rep.pn_count == 40 and rep.placement_pns == 24 and rep.extra_pns == 16
    assert sum(len(l.designators) for l in basket.lines) == 44
    assert rep.duplicate_pns == {} and rep.designator_conflicts == {}


def test_basket_parts_mapping_adds_nothing(basket):
    assert not any(l.sheet == "Parts mapping" for l in basket.lines)


def test_basket_skips_junk(basket):
    sk = {(s.sheet, s.row): s.reason for s in load_report(basket).skipped}
    assert ("Basket", 29) in sk                       # totals row
    assert ("Basket", 31) in sk                       # prose row
    assert "N/A" in sk[("Consumables", 7)]            # stencil, not a Mouser part
    assert not any("N/A" in l.mouser_pn for l in basket.lines)


def test_basket_designator_cell_split(basket):
    ln = basket.by_mouser["902-DEMO-C100N"][0]
    assert ln.designators == ("C2", "C3", "C4", "C7", "C11", "C13", "C18", "C19", "C20")
    assert (ln.sheet, ln.row) == ("Basket", 6)


# ---------------------------------------------------------------- resolve (basket)
def test_resolve_match(basket):
    r = resolve(["901-DEMO-R2K2"], basket)
    assert r.status is Status.MATCH and r.designators == ("R4", "R5")


def test_resolve_tuning_kit_no_placement(basket):
    r = resolve(["920-DEMO-R560"], basket)   # 560R R6 neighbour (TBR-3)
    assert r.status is Status.MATCH_NO_PLACEMENT and r.designators == ()
    assert r.lines[0].sheet == "Tuning kit"


def test_resolve_consumable_no_placement(basket):
    assert resolve(["930-DEMO-PASTE"], basket).status is Status.MATCH_NO_PLACEMENT


def test_resolve_demo_label(basket):
    r = resolve(LABEL, basket)
    assert r.status is Status.MATCH
    assert r.mouser_pn == "901-DEMO-R2K2"
    assert r.designators == ("R4", "R5")
    assert r.raw_codes == tuple(LABEL)


@pytest.mark.parametrize("cust", ["DEMO1 R4", "DEMO1 R4,5", "R5", "DEMO1 C2,3,4+", "", "X"])
def test_cust_pn_never_influences_result(basket, cust):
    """D7: designators come from the BOM only, whatever the Cust P/N says
    (the user truncates it in the cart)."""
    codes = [c.replace("DEMO1 R4,5", cust) for c in LABEL]
    r = resolve(codes, basket)
    assert r.status is Status.MATCH and r.designators == ("R4", "R5")
    assert r.mouser_pn == "901-DEMO-R2K2"


def test_cust_pn_alone_is_no_candidate(basket):
    assert resolve(["DEMO1 R4,5", "R4", "C2"], basket).status is Status.NO_CANDIDATE


def test_resolve_not_in_bom(basket):
    r = resolve(["40000001", "603-XXXXXXX"], basket)
    assert r.status is Status.NOT_IN_BOM and r.mouser_pn == "603-XXXXXXX"


def test_resolve_no_candidate(basket):
    r = resolve(["40000001", "30"], basket)
    assert r.status is Status.NO_CANDIDATE and r.mouser_pn is None


def test_resolve_normalizes(basket):
    assert resolve(["  901-demo-r2k2 "], basket).status is Status.MATCH


# ---------------------------------------------------------------- order BOM
def test_order_load(order_bom):
    rep = load_report(order_bom)
    roles = {s.sheet: s.role for s in rep.sheets}
    assert roles["BOM"] == "placement"
    assert roles["Order"] == "extras" and roles["Kit and Jig"] == "extras"
    assert order_bom.by_mouser["940-DEMO-C22U"][0].designators == ("C5", "C22")
    # 'Customer # (cart)' on the Order sheet is never a designator column
    assert all(l.designators == () for l in order_bom.lines if l.sheet == "Order")
    sk = {(s.sheet, s.row): s.reason for s in rep.skipped}
    for row in (12, 13, 14, 15, 16):                   # DNF rows with '-' P/N
        assert "DNF" in sk[("BOM", row)]


def test_order_merge_across_sheets(order_bom):
    r = resolve(["940-DEMO-C22U"], order_bom)
    assert r.status is Status.MATCH and r.designators == ("C5", "C22")
    assert {l.sheet for l in r.lines} == {"BOM", "Order"}


def test_order_dnf_excluded(order_bom):
    r = resolve(["950-DEMO-CONN"], order_bom)        # J2, DNF
    assert r.status is Status.MATCH_NO_PLACEMENT and r.designators == ()


# ---------------------------------------------------------------- CSV / overrides
def test_csv_and_column_map(tmp_path):
    p = tmp_path / "b.csv"
    p.write_text('Ref,Order code,Fit\n"R1, R2",791-AAA1,\nC9,81-BBB2,DNF\n,77-CCC3,\n')
    with pytest.raises(ValueError):
        load_bom(p, sheet="b")                           # no recognisable header
    b = load_bom(p, column_map={"designators": "Ref", "mouser_pn": "Order code", "fitted": "Fit"})
    assert resolve(["791-AAA1"], b).designators == ("R1", "R2")
    assert resolve(["81-BBB2"], b).status is Status.MATCH_NO_PLACEMENT
    assert resolve(["77-CCC3"], b).status is Status.MATCH_NO_PLACEMENT


def test_csv_auto_detect(tmp_path):
    p = tmp_path / "b.csv"
    p.write_text("Title line\n\nDesignators,Mouser P/N\n\"C1,C2\",81-X1\nTotal,\n")
    b = load_bom(p)
    assert resolve(["81-X1"], b).designators == ("C1", "C2")
    assert [s.row for s in load_report(b).skipped] == [5]


def test_designator_conflict_reported(tmp_path):
    p = tmp_path / "b.csv"
    p.write_text("Designators,Mouser P/N\nR6,667-A1\nR6,667-B2\n")
    assert load_report(load_bom(p)).designator_conflicts == {"R6": ["667-A1", "667-B2"]}
