"""Name resolution and selection plans (KC-13..KC-18, KC-44, KC-45): pure, over the synthetic board."""
import pytest

from polybench.kicad import select


def test_exact_reference_case_insensitive_kicad_spelling(snap):
    p = select.plan_designators(snap, ["r9", "C12"])
    assert p.parts == ["R9", "C12"] and p.parts_missing == []          # KC-13: KiCad's own spelling


def test_unknown_names_missing_and_nothing_to_select(snap):
    p = select.plan_designators(snap, ["ZZ99"])
    assert p.ids == [] and p.parts_missing == ["ZZ99"]                  # KC-14


def test_net_name_selects_every_pad(snap):
    p = select.plan_designators(snap, ["VIN"])
    assert p.nets == ["VIN"] and len(p.ids) == sum(1 for f in snap.footprints for q in f.pads if q.net == "VIN")   # KC-15


@pytest.mark.parametrize("bad", ["R9;X", "R9 C12", "'; DROP", "../../x", "EXC+0"])
def test_text_is_never_a_command(snap, bad):
    p = select.plan_designators(snap, [bad])
    assert p.ids == [] and p.parts_missing == [bad]                      # KC-16: only ids from a read are selected; EXC+0 simply is not on this board


def test_targets_parts_pins_nets_one_selection(snap):
    p = select.plan_targets(snap, ["R9"], ["IC1.10"], ["3V3_A"])
    assert p.parts == ["R9"] and p.pins == ["IC1.10"] and p.nets == ["3V3_A"]
    labels = {p.labels[i] for i in p.ids}
    assert "R9" in labels and "IC1.10" in labels
    n5 = {f"{f.ref}.{q.number}" for f in snap.footprints for q in f.pads if q.net == "3V3_A" and f.ref != "R9"}
    assert n5 <= labels and "R9.2" not in labels                          # R9's own 3V3_A pad is covered by the footprint


def test_pin_does_not_select_its_footprint(snap):
    p = select.plan_targets(snap, [], ["IC1.10"], [])
    assert p.parts == [] and [p.labels[i] for i in p.ids] == ["IC1.10"]


def test_mixed_found_and_missing_lists(snap):
    p = select.plan_targets(snap, [], ["IC1.10", "IC1.99", "QQ1.1"], ["3V3_A", "SIG_P"])
    assert (p.pins, p.pins_missing, p.nets, p.nets_missing) == (["IC1.10"], ["IC1.99", "QQ1.1"], ["3V3_A"], ["SIG_P"])   # KC-18


def test_no_inference_of_importer_suffix(snap):
    p = select.plan_targets(snap, ["CLK", "5V"], [], [])
    assert p.parts == [] and p.parts_missing == ["CLK", "5V"] and p.ids == []        # KC-44: CLK is not TP1


def test_alias_applied_before_matching_and_shown(snap):
    p = select.plan_targets(snap, ["CLK"], ["CLK.TP"], ["Z"], aliases={"clk": "TP1", "Z": "3V3_A"})
    assert p.parts == ["TP1"] and p.pins == ["CLK.TP"] and p.nets == ["3V3_A"]
    assert p.aliased == {"CLK": "TP1", "Z": "3V3_A"} and "CLK→TP1" in p.alias_note                      # KC-45


def test_alias_to_nothing_is_still_missing_and_not_reported_as_used(snap):
    p = select.plan_targets(snap, ["CLK"], aliases={"CLK": "NOPE9"})
    assert p.parts_missing == ["CLK"] and p.aliased == {}


def test_empty_alias_table_changes_nothing(snap):
    assert select.plan_targets(snap, ["TP1"], aliases={}).parts == ["TP1"]
    assert select.norm_aliases("junk") == {} and select.norm_aliases({1: 2}) == {}


def test_pads_of_selected_footprint_are_left_out(snap):
    p = select.plan_targets(snap, ["IC1"], ["IC1.3"], ["GND"])
    ic1 = snap.by_ref()["IC1"]
    assert not (set(p.ids) & {q.kiid for q in ic1.pads}) and ic1.kiid in p.ids and p.covered


def test_alias_never_overrides_an_exact_match(snap):
    """The plan's net '5V' is a real board net while the test pad TP2 is aliased from '5V'. Exact match first."""
    al = {"5V": "TP2", "VIN": "TP1"}
    p = select.plan_targets(snap, ["5V"], [], ["5V"], aliases=al)
    assert p.parts == ["TP2"] and p.nets == ["5V"] and p.nets_missing == [] and p.aliased == {"5V": "TP2"}
    q = select.plan_targets(snap, ["VIN"], [], ["VIN"], aliases=al)
    assert q.parts == ["TP1"] and q.nets == ["VIN"]
    r = select.plan_targets(snap, ["R9"], ["R9.1"], [], aliases={"R9": "C12"})
    assert r.parts == ["R9"] and r.pins == ["R9.1"] and r.aliased == {}              # an existing reference is never remapped
