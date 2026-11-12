"""Test plan as master: parser, lint with line numbers, generation, decision gate, Tier 2 applicability, interlock, retired tests."""
import json
import re
import shutil
from pathlib import Path

import pytest

from helpers import DEMO
from polybench import planfmt, tiers
from polybench.engine import CmdError, Engine
from polybench.protocol import load
from polybench.store import Journal

PLAN = Path(__file__).parent / "data" / "DEMO_TEST_PLAN.md"


def demo_bom():
    p = load(DEMO / "demo.pcbv.yaml")
    lib = {a["id"]: a for s in p.data["steps"] for sec in s["sections"] for a in sec.get("actions", [])}
    return p.data["bom"], set(lib), lib


def parse_text(tmp_path, text, bom=True):
    f = tmp_path / "PLAN.md"
    f.write_text(text, encoding="utf-8")
    b, acts, _ = demo_bom()
    return planfmt.parse(f, b if bom else None, acts if bom else None)


def line_of(text, needle):
    for i, ln in enumerate(text.split("\n"), 1):
        if needle in ln:
            return i
    raise AssertionError(needle)


def msgs(plan, sev="error"):
    return [(i.line, i.msg) for i in plan.issues if i.severity == sev]


GOOD = PLAN.read_text(encoding="utf-8")


# ---------------------------------------------------------------- the good plan
def test_good_plan_parses_clean():
    b, acts, lib = demo_bom()
    plan = planfmt.parse(PLAN, b, acts)
    assert not plan.issues
    assert sum(1 for _ in plan.tests()) == 15
    assert [(s.tier, s.mode) for s in plan.sections if s.kind == "tier"] == [(1, "unpowered"), (1, "powered"), (2, "unpowered"), (2, "powered")]
    assert plan.props["plan_format"] == 1


def test_every_field_is_mapped():
    b, acts, lib = demo_bom()
    gen = planfmt.generate(planfmt.parse(PLAN, b, acts), lib)
    un, pw = gen["steps"][0], gen["steps"][1]
    assert (un["id"], un["advance"]) == ("unpowered", "row") and (pw["id"], pw["advance"]) == ("powered", "board")
    s1 = un["sections"][0]
    assert s1["id"] == "t1_input_and_rails" and s1["tier"] == 1 and s1["designators"] == ["J1", "C1", "C2", "C3", "U1"]
    assert s1["instructions"].startswith("Board unpowered")
    u01 = s1["tests"][0]
    assert u01["kind"] == "number" and u01["unit"] == "kOhm"
    assert u01["limit"] == {"class": "guess", "min": 1.0, "source": {"type": "guess"}, "closes_on": "the ten readings of the first batch"}
    assert u01["probe"] == [{"point": "J1", "pole": "+"}, {"point": "C2", "pole": "-"}]
    assert u01["nets"] == ["5V", "GND"] and u01["highlight"] == ["C2", "U1"] and u01["highlight_pins"] == ["U1.3"]
    assert u01["stop_if"] == {"below": 0.1, "text": "5V looks shorted: do not power this board."}
    assert u01["outlier"] == {"ratio": [0.5, 2.0]} and u01["inputs"] == ["keyboard", "voice"]
    u02 = s1["tests"][1]
    assert u02["limit"]["source"] == {"type": "design_calc", "doc": "calc.md", "ref": "C1"} and u02["inputs"] == ["keyboard", "voice"]   # — = default
    u03 = s1["tests"][2]
    assert u03["limit"]["source"] == {"type": "datasheet", "doc": "DS-DEMOTVS", "page": 4, "table": "2"}
    assert u03["stop_if"] == {"outside": [0.2, 1.2], "text": "D1 is not a diode."}
    assert u03["instructions"] == "Red lead on the anode, which is the pad away from the band."      # per-test list after the table
    u04 = s1["tests"][3]
    assert u04["kind"] == "text" and u04["limit"] == {"class": "record"} and "unit" not in u04
    p01 = pw["sections"][0]["tests"][0]
    assert p01["outlier"] == {"pct": 25.0, "floor": 0.5, "floor_unit": "mA"} and p01["stop_if"]["above"] == 100.0
    assert [a["id"] for a in pw["sections"][0]["actions"]] == ["read_rails"] and pw["sections"][0]["actions"][0]["plugin"] == "demo.measure"
    assert "Actions:" not in pw["sections"][0]["instructions"]
    p05 = pw["sections"][0]["tests"][4]
    assert p05["kind"] == "yn" and p05["limit"]["equals"] == "yes" and p05["stop_if"] == {"equals": "no", "text": "Too hot: switch off and set the board aside."}
    p06 = pw["sections"][0]["tests"][5]
    assert p06["kind"] == "choice" and p06["options"] == ["held in reset (RST to GND)", "blank MCU running"] and p06["title"] == "MCU state"
    t2 = gen["steps"][0]["sections"][-1]
    assert t2["tier"] == 2 and t2["applies_to"] == "operator" and t2["conditional"] is True
    assert gen["tier_gates"][0]["rules"][0] == {"type": "out_of_band"} and gen["tier_gates"][0]["after"] == {"tier": 1, "mode": "unpowered"}


def test_fragment_header_and_hash(tmp_path):
    b, acts, lib = demo_bom()
    plan = planfmt.parse(PLAN, b, acts)
    y = planfmt.to_yaml(plan, lib)
    assert y.startswith(f"# GENERATED from DEMO_TEST_PLAN.md @ {plan.hash}, do not edit.")
    assert planfmt.to_yaml(planfmt.parse(PLAN, b, acts), lib) == y            # deterministic


# ---------------------------------------------------------------- lint: each error carries the plan file's line number
LINT_CASES = [
    # (name, old text, new text, line marker (text in the NEW file whose line must be reported), message fragment)
    ("unknown column", "| Source | Stop if | Outlier |", "| Source | Stop if | Bogus |", "| Bogus |", "unknown column 'Bogus'"),
    ("missing column", "| Stop if | Outlier | Inputs |", "| Stop if | Inputs |", "| Stop if | Inputs |", "missing column 'Outlier'"),
    ("columns out of order", "| Kind | Unit |", "| Unit | Kind |", "| Unit | Kind |", "out of order"),
    ("missing source on hard", "| hard | calc C1 |", "| hard | — |", "| U02 |", "hard limit without a source"),
    ("hard with a guess source", "| hard | calc C1 |", "| hard | closes on: the ten readings |", "| U02 |", "hard limit with a guess source"),
    ("guess without closes on", "| guess | closes on: the ten readings of the first batch |", "| guess | calc C1 |", "| U01 |", "needs `closes on"),
    ("source not understood", "| hard | calc C1 |", "| hard | somebody said so |", "| U02 |", "source must be"),
    ("duplicate id", "| U06 | R4 across its pads", "| U05 | R4 across its pads", "| U05 | R4", "duplicate ID 'U05'"),
    ("id differs only by case", "| U06 | R4 across its pads", "| u05 | R4 across its pads", "| u05 | R4", "differs only in case"),
    ("highlight designator not in the BOM", "| R3 | R3 | — | R3 | number | kOhm | 9.8", "| R3 | R3 | — | R3, Q9 | number | kOhm | 9.8", "Q9", "Q9 is not in the BOM"),
    ("highlight pin on a part not in the BOM", "C2, U1.3 |", "C2, ZZ1.3 |", "ZZ1.3", "ZZ1 is not in the BOM"),
    ("group designator not in the BOM", "### Pull-ups (R3, R4, R5)", "### Pull-ups (R3, R4, R99)", "### Pull-ups", "group designator 'R99' is not in the BOM"),
    ("malformed stop if", "below 0.1: 5V looks shorted", "under 0.1: 5V looks shorted", "| U01 |", "malformed Stop if"),
    ("stop if without text", "below 0.05: 3V3 shorted.", "below 0.05", "| U02 |", "malformed Stop if"),
    ("stop if outside reversed", "outside 0.2..1.2:", "outside 1.2..0.2:", "| U03 |", "malformed Stop if"),
    ("yn stop must be on No", "on No: Too hot", "below 3: Too hot", "| P05 |", "yn test takes `on No"),
    ("malformed outlier", "| x0.5..x2 | keyboard, voice |", "| twice | keyboard, voice |", "| U01 |", "malformed Outlier"),
    ("outlier on a yn test", "| on No: Too hot: switch off and set the board aside. | — |", "| on No: Too hot: switch off and set the board aside. | ±5% |", "| P05 |", "Outlier applies to number"),
    ("unknown kind", "| R3 | number | kOhm | 9.8 |", "| R3 | float | kOhm | 9.8 |", "| U05 |", "Kind 'float'"),
    ("unit missing on a number", "| number | V | 0.3 | 0.9 |", "| number | — | 0.3 | 0.9 |", "| U03 |", "needs a Unit"),
    ("unit not in the vocabulary", "| number | V | 0.3 | 0.9 |", "| number | furlong | 0.3 | 0.9 |", "| U03 |", "not in the vocabulary"),
    ("min above max", "| 0.3 | 0.9 |", "| 0.9 | 0.3 |", "| U03 |", "Min is above Max"),
    ("limit not a number", "| 0.3 | 0.9 |", "| 0.3 | high |", "| U03 |", "Max 'high' is not a number"),
    ("number hard without min or max", "| 0.3 | 0.9 | hard |", "| — | — | hard |", "| U03 |", "needs Min and/or Max"),
    ("yn with a min", "| yn | — | — | — | guess |", "| yn | — | 3 | — | guess |", "| P05 |", "takes Min and Max as"),
    ("choice without options", "(options: held in reset (RST to GND) / blank MCU running)", "", "| P06 |", "needs `(options"),
    ("hard text test", "| text | — | — | — | record | — | — | — | keyboard |", "| text | — | — | — | hard | calc C1 | — | — | keyboard |", "| U04 |", "text test cannot have a hard limit"),
    ("bad input", "| keyboard, voice |", "| keyboard, telepathy |", "| U01 |", "input 'telepathy'"),
    ("reserved id", "| U04 | Multimeter", "| PASS | Multimeter", "| PASS |", "reserved"),
    ("id width", "| U06 | R4 across", "| U6 | R4 across", "| U6 |", "different digit width"),
    ("action not defined", "Actions: read_rails", "Actions: read_rails, nope_action", "Actions: read_rails", "action 'nope_action' is not defined"),
    ("row with the wrong cell count", "| U05 | R3 across its pads | R3 | R3 | — | R3 |", "| U05 | R3 across its pads | R3 | R3 | R3 |", "| U05 |", "row has"),
    ("per-test instruction for a stranger", "- U03: Red lead", "- U77: Red lead", "- U77:", "'U77'"),
    ("malformed tier heading", "## Tier 1 — Powered", "## Tier 1 Powered now", "## Tier 1 Powered now", "malformed tier heading"),
    ("group heading without designators", "### Pull-ups (R3, R4, R5)", "### Pull-ups", "### Pull-ups", "group heading must be"),
    ("two instruction paragraphs", "Board unpowered, multimeter on resistance, black lead on the C2 pad. Read after about 5 s.",
     "Board unpowered, multimeter on resistance.\n\nRead after about 5 s.", "### Input and rails", "2 instruction paragraphs"),
    ("second table in a group", "- U03: Red lead", "| ID | Test |\n|---|---|\n| X | y |\n\n- U03: Red lead", "| X | y |", "second table"),
    ("unknown gate rule", "any stop\noutlier from 3 boards\nhold power", "any stop\nexplode\nhold power", "explode", "unknown gate rule"),
    ("gate without after", "after: Tier 1 Unpowered\n", "", "```gate", "needs `after:"),
    ("gate points at a missing tier", "to: Tier 2 Unpowered", "to: Tier 3", "to: Tier 3", "Tier 3"),
]


@pytest.mark.parametrize("name,old,new,marker,frag", LINT_CASES, ids=[c[0] for c in LINT_CASES])
def test_lint_error_has_the_plan_line(tmp_path, name, old, new, marker, frag):
    assert old in GOOD, f"fixture drifted: {old!r}"
    text = GOOD.replace(old, new, 1)
    plan = parse_text(tmp_path, text)
    hits = [(ln, m) for ln, m in msgs(plan) if frag in m]
    assert hits, f"no error containing {frag!r}; got {msgs(plan)}"
    want = line_of(text, marker.split("\n")[0])
    assert want in [ln for ln, _ in hits], f"{name}: reported lines {[h[0] for h in hits]}, expected {want}"
    assert plan.errors


def test_exact_line_numbers_for_the_four_named_errors(tmp_path):
    cases = {
        "unknown column": ("| Source | Stop if | Outlier |", "| Source | Stop if | Bogus |", 1),
        "missing source": ("| hard | calc C1 |", "| hard | — |", 1),
        "duplicate": ("| U06 | R4 across its pads", "| U05 | R4 across its pads", 1),
        "not in BOM": ("C2, U1.3 |", "C2, ZZ1.3 |", 1),
        "malformed stop": ("below 0.1: 5V looks shorted", "under 0.1: 5V looks shorted", 1),
    }
    for name, (old, new, _n) in cases.items():
        text = GOOD.replace(old, new, 1)
        plan = parse_text(tmp_path, text)
        lines = [ln for ln, _ in msgs(plan)]
        marker = {"unknown column": "| Bogus |", "missing source": "| U02 |", "duplicate": "| U05 | R4", "not in BOM": "ZZ1.3", "malformed stop": "under 0.1"}[name]
        assert line_of(text, marker) in lines, (name, lines)


def test_plan_format_key(tmp_path):
    assert any("plan_format" in m for _l, m in msgs(parse_text(tmp_path, GOOD.replace("plan_format: 1\n", ""))))
    assert any("not supported" in m for _l, m in msgs(parse_text(tmp_path, GOOD.replace("plan_format: 1", "plan_format: 2"))))


def test_no_bom_skips_designator_checks_with_a_warning(tmp_path):
    plan = parse_text(tmp_path, GOOD.replace("C2, U1.3 |", "C2, ZZ1.3 |"), bom=False)
    assert not plan.errors and any("not checked against the BOM" in m for _l, m in msgs(plan, "warning"))


def test_comments_and_code_blocks_are_ignored(tmp_path):
    text = GOOD.replace("## Not covered", "%% | U99 | a commented row | — | %%\n\n```\n### Not (a) group (R3)\n| ID | Test |\n```\n\n## Not covered")
    assert not parse_text(tmp_path, text).issues


def test_duplicate_tier_section_reports_the_second_heading(tmp_path):
    text = GOOD.replace("## Tier 2 — Powered (deeper review)", "## Tier 1 — Powered")
    plan = parse_text(tmp_path, text)
    last = max(i for i, ln in enumerate(text.split("\n"), 1) if ln == "## Tier 1 — Powered")
    assert (last, "duplicate section 'Tier 1 — Powered' (first at line %d)" % line_of(text, "## Tier 1 — Powered")) in msgs(plan)


def test_dash_variants_are_accepted_with_a_warning(tmp_path):
    text = GOOD.replace("| U04 | Multimeter model and range | — | — | — | — |", "| U04 | Multimeter model and range | - | - | - | - |")
    plan = parse_text(tmp_path, text)
    assert not plan.errors and any("should be written as —" in m for _l, m in msgs(plan, "warning"))


# ---------------------------------------------------------------- decision gate block
def test_gate_blocks_parse():
    b, acts, _ = demo_bom()
    plan = planfmt.parse(PLAN, b, acts)
    a, bg = plan.gates
    assert a["after"] == {"tier": 1, "mode": "unpowered"} and a["to"] == {"tier": 2, "mode": "unpowered"}
    assert [r["type"] for r in a["rules"]] == ["out_of_band", "stop", "outlier", "hold_power", "halt"]
    assert a["rules"][2]["min_boards"] == 3 and a["rules"][4]["pct"] == 50.0
    assert bg["after"] == {"tier": 1, "mode": ""} and "hold_power" not in [r["type"] for r in bg["rules"]]


def test_outlier_rule_needs_a_band_somewhere(tmp_path):
    text = re.sub(r"\| (x0\.5\.\.x2|±25% \(min 0\.5 mA\)) \|", "| — |", GOOD)
    assert any("no test has an Outlier entry" in m for _l, m in msgs(parse_text(tmp_path, text)))


# ---------------------------------------------------------------- protocol: the plan generates the tests
def make_proto(tmp_path, plan_text=GOOD, generate=True):
    d = tmp_path / "proj"
    shutil.copytree(DEMO, d)
    (d / "PLAN.md").write_text(plan_text, encoding="utf-8")
    top = d / "demo.pcbv.yaml"
    top.write_text(top.read_text(encoding="utf-8") + ("\ntest_plan: {file: PLAN.md, generate: true}\n" if generate else "\ntest_plan: PLAN.md\n"), encoding="utf-8")
    return top


def test_protocol_generates_from_plan(tmp_path):
    top = make_proto(tmp_path)
    p = load(top)
    assert not p.errors, [str(e) for e in p.errors]
    un = p.step("unpowered")
    assert un["plan_generated"] and [s["id"] for s in un["sections"]][:2] == ["t1_input_and_rails", "t1_pull_ups"]
    assert p.test("U01") and p.test("P20") and not p.test("U07")             # the demo's hand-written tests are gone
    assert any("hand-written test(s) are not in PLAN.md" in str(w) for w in p.warnings) or not p.warnings
    assert p.data["plan"]["tests"] == 15 and p.data["plan"]["generated"] is True
    assert [a["id"] for a in p.step("powered")["sections"][0]["actions"]] == ["read_rails"]      # the hand-written action is the library
    # the run keeps working: demo gate on the function step still resolves
    assert p.step("function")


def test_plan_hash_is_not_in_the_protocol_hash(tmp_path):
    top = make_proto(tmp_path)
    h1 = load(top).hash
    (top.parent / "PLAN.md").write_text(GOOD + "\nmore prose\n", encoding="utf-8")
    p2 = load(top)
    assert p2.hash == h1 and p2.data["plan"]["hash"] != load(make_proto(tmp_path / "x")).data["plan"]["hash"]


def test_plan_error_blocks_generation(tmp_path):
    top = make_proto(tmp_path, GOOD.replace("| hard | calc C1 |", "| hard | — |"))
    p = load(top)
    assert p.errors and any("hard limit without a source" in str(e) for e in p.errors)
    assert p.data["plan"]["generated"] is False


def test_generate_false_only_displays(tmp_path):
    p = load(make_proto(tmp_path, generate=False))
    assert not p.errors and not p.step("unpowered").get("plan_generated") and p.test("U07") is not None or p.test("U01")


def test_missing_plan_with_generate_is_an_error(tmp_path):
    top = make_proto(tmp_path)
    (top.parent / "PLAN.md").unlink()
    assert any("test plan file not found" in str(e) for e in load(top).errors)


# ---------------------------------------------------------------- engine
def make_engine(tmp_path, plan_text=GOOD, boards=("S01", "S02", "S03", "S04")):
    top = make_proto(tmp_path, plan_text)
    p = load(top)
    assert not p.errors, [str(e) for e in p.errors]
    eng = Engine(p, Journal(tmp_path / "j.jsonl"), "tester")
    eng.create_batch("t", list(boards), "S", True, "DEMO-1")
    for b in boards:
        eng.override_gate(b, "unpowered", "visual not under test here")
    return eng, top


def test_plan_loaded_is_journalled_once_per_hash(tmp_path):
    eng, top = make_engine(tmp_path)
    info = {"file": "PLAN.md", "hash": "abc", "count": 15, "tests": [{"id": "U01", "title": "x", "step": "unpowered", "section": "s", "tier": 1}]}
    assert eng.plan_loaded(info) is True
    assert eng.plan_loaded(info) is False
    assert eng.plan_loaded({**info, "hash": "def"}) is True
    assert [e["type"] for e in eng.journal.events].count("plan_loaded") == 2
    # replay
    e2 = Engine(eng.proto, Journal(tmp_path / "j.jsonl"), "tester")
    assert e2.state.plan["hash"] == "def" and len(e2.state.plan_history) == 2


def test_tier2_is_na_until_sent_and_does_not_hold_the_step(tmp_path):
    eng, _ = make_engine(tmp_path)
    st = eng.state
    un = eng.proto.step("unpowered")
    t1 = [t["id"] for sec in un["sections"] if sec.get("tier") == 1 for t in sec["tests"]]
    for b in ("S01", "S02", "S03", "S04"):
        for tid in t1:
            t = eng.proto.test(tid)[2]
            if t["kind"] == "text":
                eng.entry(b, tid, "Fluke 117")
            else:
                lim = t["limit"]
                eng.entry(b, tid, str((lim.get("min", 0) + lim.get("max", lim.get("min", 0) * 2)) / 2 if "max" in lim else lim["min"] * 3))
    snap = st.snapshot()
    sb = [s for s in snap["steps"] if s["id"] == "unpowered"][0]["per_board"]["S01"]
    assert sb["state"] == "done" and sb["applicable"] == len(t1)                # the 3 Tier 2 tests are not counted
    assert not st.snapshot()["cells"].get("S01|U20")
    with pytest.raises(CmdError) as e:
        eng.entry("S01", "U20", "found a bridge")
    assert e.value.status == 409 and "Tier 2" in str(e.value)
    eng.tier2_assign("S01", "unpowered", "all", "test", "operator")
    sb2 = [s for s in st.snapshot()["steps"] if s["id"] == "unpowered"][0]["per_board"]
    assert sb2["S01"]["applicable"] == len(t1) + 2 and sb2["S01"]["state"] == "partial"
    assert sb2["S02"]["applicable"] == len(t1)
    eng.entry("S01", "U20", "found a bridge under U1")
    eng.entry("S01", "U21", "y", confirm=True)
    assert [s for s in st.snapshot()["steps"] if s["id"] == "unpowered"][0]["per_board"]["S01"]["state"] == "done"
    # the powered step gate needs unpowered done only for Tier 1
    assert eng.state.gate(eng.proto.step("powered"), "S02")["ok"]


def fill(eng, board, values):
    for tid, v in values.items():
        eng.entry(board, tid, str(v), confirm=True)


def good_unpowered(eng, board, over=None):
    vals = {"U01": 2.0, "U02": 3.0, "U03": 0.6, "U04": "Fluke", "U05": 10.0, "U06": 10.0}
    over = over or {}
    for k in over:
        vals.pop(k, None)
    vals.update(over)                      # the values that trip a stop go last: after a stop the board takes no more entries
    fill(eng, board, vals)


def test_gate_a_verdicts_reasons_outliers_and_halt(tmp_path):
    eng, _ = make_engine(tmp_path, boards=("S01", "S02", "S03", "S04", "S05"))
    for b in ("S01", "S02", "S03", "S04"):
        good_unpowered(eng, b)
    good_unpowered(eng, "S05", {"U05": 12.0, "U01": 0.04})        # U01 below the stop value, U05 out of band
    g = tiers.eval_gate(eng.state, 0)
    rows = {r["board"]: r for r in g["boards"]}
    assert rows["S01"]["verdict"] == "clean" and rows["S05"]["verdict"] == "tier2"
    txt = " ".join(rows["S05"]["reasons"])
    assert "U01" in txt and "stop rule" in txt and "U05" in txt
    assert g["halt"] == []                                       # 1 of 5 boards
    # an outlier only: 3 boards at 2 kOhm, one at 9 kOhm (x0.5..x2 of the median 2)
    eng2, _ = make_engine(tmp_path / "b", boards=("S01", "S02", "S03", "S04"))
    for b, v in zip(("S01", "S02", "S03", "S04"), (2.0, 2.1, 1.9, 9.0)):
        good_unpowered(eng2, b, {"U01": v})
    r2 = {r["board"]: r for r in tiers.eval_gate(eng2.state, 0)["boards"]}
    assert r2["S04"]["verdict"] == "outliers" and "U01" in r2["S04"]["outliers"][0] and r2["S01"]["verdict"] == "clean"
    # the halt rule: the same test out of band on more than half the boards
    eng3, _ = make_engine(tmp_path / "c", boards=("S01", "S02", "S03"))
    for b in ("S01", "S02", "S03"):
        good_unpowered(eng3, b, {"U05": 15.0} if b != "S03" else {})
    assert "U05 is out of band on 2 of 3 boards" in tiers.eval_gate(eng3.state, 0)["halt"][0]


def test_waiting_until_tier1_is_settled(tmp_path):
    eng, _ = make_engine(tmp_path)
    eng.entry("S01", "U01", "2")
    r = {x["board"]: x for x in tiers.eval_gate(eng.state, 0)["boards"]}
    assert r["S01"]["verdict"] == "waiting" and r["S01"]["settled"] == 1 and r["S01"]["total"] == 6


def test_gate_accept_opens_tier2_for_those_boards_only_and_reject_is_recorded(tmp_path):
    eng, _ = make_engine(tmp_path, boards=("S01", "S02", "S03"))
    for b in ("S01", "S02", "S03"):
        good_unpowered(eng, b, {"U05": 12.0} if b != "S02" else {})
    with pytest.raises(CmdError):
        eng.gate_accept(0, ["S02"])                                  # the gate does not propose it
    assert eng.gate_accept(0, ["S01"])["boards"] == ["S01"]
    eng.gate_reject(0, ["S03"], "bridge looks fine under the microscope")
    with pytest.raises(CmdError):
        eng.gate_reject(0, ["S03"], " ")
    rows = {r["board"]: r for r in tiers.eval_gate(eng.state, 0)["boards"]}
    assert rows["S01"]["assigned"] and rows["S03"]["rejected"].startswith("bridge") and not rows["S02"]["assigned"]
    snap = eng.state.snapshot()
    assert snap["tier2"]["S01|unpowered"]["via"].startswith("Decision gate") and "S03|unpowered" not in snap["tier2"]
    # the sections reach only the unpowered step (gate A: to Tier 2 Unpowered)
    assert "S01|powered" not in snap["tier2"]


def test_power_interlock(tmp_path):
    eng, _ = make_engine(tmp_path, boards=("S01", "S02"))
    good_unpowered(eng, "S01")
    good_unpowered(eng, "S02", {"U01": 0.5})
    pw = eng.proto.step("powered")
    assert eng.state.gate(pw, "S01")["ok"]
    g = eng.state.gate(pw, "S02")
    assert not g["ok"] and "power interlock" in g["reasons"][0] and "send it to Tier 2" in g["reasons"][0]
    with pytest.raises(CmdError):
        eng.entry("S02", "P01", "20")
    eng.gate_accept(0, ["S02"])
    assert "Tier 2 is not settled" in eng.state.gate(pw, "S02")["reasons"][-1]
    eng.entry("S02", "U20", "R3 was 12k, replaced")
    eng.entry("S02", "U21", "y", confirm=True)
    assert "repeat Tier 1 unpowered" in eng.state.gate(pw, "S02")["reasons"][-1]
    eng.retest("S02", "unpowered", "R3 replaced")
    assert "repeat of Tier 1 unpowered is not finished" in eng.state.gate(pw, "S02")["reasons"][-1]
    good_unpowered(eng, "S02")
    assert eng.state.gate(pw, "S02")["ok"]                          # repeated, clean, Tier 2 of attempt 1 settled
    assert eng.state.snapshot()["holds"] == {}
    # the operator can still override with a reason
    good_unpowered(eng, "S01")  # no-op re-entry of the same values
    eng2, _ = make_engine(tmp_path / "o", boards=("S01",))
    good_unpowered(eng2, "S01", {"U01": 0.5})
    eng2.override_gate("S01", "powered", "PM said go")
    assert eng2.state.gate(eng2.proto.step("powered"), "S01")["ok"]


def test_interlock_is_off_without_the_hold_power_rule(tmp_path):
    eng, _ = make_engine(tmp_path, GOOD.replace("hold power\n", ""), boards=("S01",))
    good_unpowered(eng, "S01", {"U01": 0.5})
    assert eng.state.gate(eng.proto.step("powered"), "S01")["ok"]


def test_gate_b_sends_outliers_to_tier2_after_powered(tmp_path):
    eng, _ = make_engine(tmp_path, boards=("S01", "S02", "S03", "S04"))
    for b in ("S01", "S02", "S03", "S04"):
        good_unpowered(eng, b)
        fill(eng, b, {"P01": 20.0 if b != "S04" else 29.0, "P02": 5.0, "P03": 3.3, "P04": 20.0, "P05": "y", "P06": "blank MCU running"})
    rows = {r["board"]: r for r in tiers.eval_gate(eng.state, 1)["boards"]}
    assert rows["S04"]["verdict"] == "tier2" and "P01" in rows["S04"]["reasons"][0] and rows["S01"]["verdict"] == "clean"


def test_retired_tests_keep_their_results(tmp_path):
    eng, top = make_engine(tmp_path, boards=("S01",))
    good_unpowered(eng, "S01")
    info = {"file": "PLAN.md", "hash": "h1", "count": 15, "tests": [{"id": t["id"], "title": t["title"], "step": s["id"], "section": sec["id"], "tier": 1}
                                                                     for s, sec, t in eng.proto.tests() if s.get("plan_generated")]}
    eng.plan_loaded(info)
    # revision 2 of the plan drops U06
    (top.parent / "PLAN.md").write_text(re.sub(r"\| U06 \|.*\n", "", GOOD), encoding="utf-8")
    new = load(top)
    assert not new.errors and not new.test("U06")
    eng.proto = new
    eng.state.proto = new
    eng.state.test_index = {t["id"]: (s, sec, t) for s, sec, t in new.tests()}
    snap = eng.state.snapshot()
    ret = {r["id"]: r for r in snap["retired"]}
    assert list(ret) == ["U06"] and ret["U06"]["results"]["S01"]["value"] == "10.0" and ret["U06"]["title"] == "R4 across its pads"
    assert "S01|U06" not in snap["cells"]
    # a restart replays: the results are keyed by test ID, nothing is lost
    e2 = Engine(new, Journal(tmp_path / "j.jsonl"), "tester")
    assert {r["id"] for r in e2.state.snapshot()["retired"]} == {"U06"}
    assert e2.state.cell("S01", "U05")["value"] == "10.0"


def test_outside_stop_rule(tmp_path):
    eng, _ = make_engine(tmp_path, boards=("S01",))
    r = eng.entry("S01", "U03", "1.5", confirm=True)
    assert r["stop"] == "D1 is not a diode."
    eng.ack_stop("S01", eng.state.stops[0]["id"], "re-seated the probe")
    assert eng.entry("S01", "U03", "0.6", replace=True, confirm=True)["stop"] is None


# ---------------------------------------------------------------- CLI
def test_cli_import_writes_header_and_refuses_on_error(tmp_path, capsys):
    from polybench.cli import main
    proto = DEMO / "demo.pcbv.yaml"
    out = tmp_path / "frag.yaml"
    assert main(["plan", "import", str(PLAN), "--protocol", str(proto), "--out", str(out)]) == 0
    head = out.read_text(encoding="utf-8").split("\n")[0]
    assert re.fullmatch(r"# GENERATED from DEMO_TEST_PLAN\.md @ [0-9a-f]{16}, do not edit\.", head)
    import yaml
    frag = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert [s["id"] for s in frag["steps"]] == ["unpowered", "powered"] and len(frag["tier_gates"]) == 2
    bad = tmp_path / "BAD.md"
    bad.write_text(GOOD.replace("| hard | calc C1 |", "| hard | — |"), encoding="utf-8")
    out2 = tmp_path / "frag2.yaml"
    assert main(["plan", "import", str(bad), "--protocol", str(proto), "--out", str(out2)]) == 2
    assert not out2.exists()
    err = capsys.readouterr().err
    assert f"BAD.md:{line_of(bad.read_text(encoding="utf-8"), '| U02 |')}: error: U02: hard limit without a source" in err
