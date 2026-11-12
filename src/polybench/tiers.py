"""Tier 1 / Tier 2 logic on top of State: which boards a Tier 2 section applies to, the decision gate verdicts, the power interlock.

All of it is derived from the journal (cells, stops, tier2_assign / gate_reject events) and from the generated protocol (`tier_gates`,
sections with `tier` / `conditional` / `applies_to: operator`, tests with `outlier`). Nothing here writes.
"""
from __future__ import annotations

import re
import statistics

OL_FLOOR = 99999.0           # the plan types 99999 for OL; such readings take no part in a median



def _band_pct(band):
    """Percent of a relative band: `pct`, or the plan's `tolerance` (a fraction, `mode: relative`)."""
    if "pct" in band:
        return band["pct"]
    return 100.0 * float(band.get("tolerance") or 0.0)

def tier_of(sec) -> int:
    return int(sec.get("tier") or 1)


def is_conditional(sec) -> bool:
    return sec.get("applies_to") == "operator"


def assigned_sections(state, step_id: str, board: str) -> set:
    a = state.assigns.get((board, step_id))
    if not a or a.get("attempt") != state.attempt(board, step_id):
        return set()
    return set(a["sections"])


def sec_applies(state, step: dict, sec: dict, board: str) -> bool:
    if not state.applies(step, board):
        return False
    if is_conditional(sec):
        return sec["id"] in assigned_sections(state, step["id"], board)
    return True


def out_of_band(cell) -> bool:
    return bool(cell) and cell.get("status") == "accepted" and cell.get("verdict") in ("fail", "review")


def _scope_tests(state, after: dict):
    """(step, sec, test) of the tests a gate looks at: tier `after.tier`, and the one step when a mode is given."""
    for s in state.proto.data["steps"]:
        if after.get("mode") and s["id"] != after["mode"]:
            continue
        for sec in s.get("sections", []):
            if sec.get("tier") != after["tier"] or is_conditional(sec):
                continue
            for t in sec.get("tests", []):
                yield s, sec, t


def _num(v):
    t = str(v).strip().upper()
    if t in ("SC", "SHORT"):
        return 0.0
    if t in ("OL", "OC", "OPEN"):
        return None              # like 99999: over-range takes no part in a median
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def outlier_check(test: dict, value, others: list):
    """-> (is_outlier, median, text) for one reading against the batch. `others` = every board's numeric reading (this one included)."""
    band = test.get("outlier")
    v = _num(value)
    if not band or v is None or v >= OL_FLOOR:
        return False, None, ""
    xs = [x for x in others if x is not None and x < OL_FLOOR]
    if not xs:
        return False, None, ""
    m = statistics.median(xs)
    unit = f" {test['unit']}" if test.get("unit") else ""
    if "ratio" in band:
        lo, hi = band["ratio"]
        if m <= 0:
            return False, m, ""
        if v < m * lo or v > m * hi:
            return True, m, f"{test['id']} {v:g}{unit} is outside x{lo:g}..x{hi:g} of the batch median {m:g}{unit}"
        return False, m, ""
    diff = abs(v - m)
    floor = band.get("floor") or 0.0
    if m != 0 and diff > abs(m) * _band_pct(band) / 100.0 and diff >= floor:
        return True, m, f"{test['id']} {v:g}{unit} differs from the batch median {m:g}{unit} by more than {_band_pct(band):g} %"
    return False, m, ""


# ---- live "not similar to the batch" highlight (display only: never a verdict, never blocks, never read by eval_gate)
# Default band by magnitude of the batch median (25-50 % depending on magnitude, wider at the low and high ends where
# measurement error grows). Resistance is converted to ohms first. One block so the numbers are easy to change.
SIM_MIN_BOARDS = 3            # numeric readings needed before anything is flagged
SIM_LOW_OHM = 10.0            # median below this: low-ohm band
SIM_LOW_PCT = 50.0
SIM_LOW_FLOOR_OHM = 0.3       # GUESS (PM proposal, not from a datasheet): lead and contact resistance dominate below 10 ohm
SIM_MID_PCT = 25.0            # 10 ohm .. 10 kohm
SIM_HIGH_OHM = 10_000.0       # median above this: high-ohm band
SIM_HIGH_PCT = 50.0
SIM_OTHER_PCT = 25.0          # V, A and any other unit

_OHM = re.compile(r"^([kKMm]?)(ohm|Ω|Ω)s?$", re.I)
_PREFIX = {"": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "m": 1e-3}


def ohm_scale(unit):
    """Factor to ohms for a resistance unit (Ohm, kOhm, MOhm, mOhm, kΩ ...), None for any other unit."""
    m = _OHM.match((unit or "").strip())
    if not m:
        return None
    pre = m.group(1)
    return _PREFIX[pre] if pre in _PREFIX else None


def _unit_disp(unit):
    return re.sub(r"ohm", "Ω", unit or "", flags=re.I)


def _reading(v):
    """-> ('num', x) | ('over', token) | None. OL / OC / open and anything >= 99999 in its unit are over-range; SC is 0."""
    t = str(v).strip().upper()
    if t in ("OL", "OC", "OPEN"):
        return "over", ("OC" if t == "OC" else "OL")
    if t in ("SC", "SHORT"):
        return "num", 0.0
    try:
        x = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None
    if x >= OL_FLOOR:
        return "over", "OL"
    return "num", x


def default_band(median: float, unit):
    """-> (pct, abs_floor, text) in the test's own unit. Median is in the test's unit; resistance is judged in ohms."""
    sc = ohm_scale(unit)
    if sc is None:
        return SIM_OTHER_PCT, 0.0, f"±{SIM_OTHER_PCT:g} %"
    ohm = abs(median) * sc
    if ohm < SIM_LOW_OHM:
        return SIM_LOW_PCT, SIM_LOW_FLOOR_OHM / sc, f"±{SIM_LOW_PCT:g} % (min {SIM_LOW_FLOOR_OHM:g} Ω)"
    if ohm > SIM_HIGH_OHM:
        return SIM_HIGH_PCT, 0.0, f"±{SIM_HIGH_PCT:g} %"
    return SIM_MID_PCT, 0.0, f"±{SIM_MID_PCT:g} %"


def _fmt(x: float) -> str:
    """4 significant digits, never an exponent."""
    return f"{float(f'{x:.4g}'):f}".rstrip("0").rstrip(".") or "0"


def _join(names):
    return " ".join(names)


def similarity(test: dict, readings_by_board: dict):
    """Display-only 'not similar to the batch' for one number test. `readings_by_board` = {board: raw value} of the current attempt.

    -> None (not a number test, or fewer than SIM_MIN_BOARDS readings) or
       {"median", "unit", "band", "n", "flagged": {board: {"value", "kind", "ratio", "text"}}}.
    The band is the plan's Outlier column when the test has one, else the default by magnitude."""
    if test.get("kind") != "number":
        return None
    nums, over = {}, {}
    for b, v in readings_by_board.items():
        r = _reading(v)
        if r is None:
            continue
        (nums if r[0] == "num" else over)[b] = r[1]
    if len(nums) + len(over) < SIM_MIN_BOARDS:
        return None
    unit = test.get("unit") or ""
    ud = _unit_disp(unit)
    u = f" {ud}" if ud else ""
    flagged: dict = {}
    # over-range against finite: the minority is flagged
    if nums and over and len(nums) != len(over):
        minority, majority = (nums, over) if len(nums) < len(over) else (over, nums)
        for b in minority:
            if minority is nums:
                txt = f"{b} reads {_fmt(nums[b])}{u} while {_join(majority)} are {next(iter(over.values()))}"
                flagged[b] = {"value": nums[b], "kind": "overrange", "ratio": None, "text": txt}
            else:
                txt = f"{b} reads {over[b]} while {_join(majority)} read finite"
                flagged[b] = {"value": over[b], "kind": "overrange", "ratio": None, "text": txt}
    median, band_text = None, ""
    if len(nums) >= SIM_MIN_BOARDS:
        median = statistics.median(nums.values())
        band = test.get("outlier")
        for b, v in nums.items():
            if "ratio" in (band or {}):
                lo, hi = band["ratio"]
                band_text = f"x{lo:g}..x{hi:g}"
                if median <= 0:
                    continue
                hit = v < median * lo or v > median * hi
            else:
                if band:
                    pct, fl = _band_pct(band), band.get("floor") or 0.0
                    fs = ohm_scale(band.get("floor_unit"))
                    ts = ohm_scale(unit)
                    if fs is not None and ts is not None:
                        fl = fl * fs / ts
                    band_text = f"±{pct:g} %" + (f" (min {band['floor']:g} {_unit_disp(band.get('floor_unit') or unit)})" if band.get("floor") else "")
                else:
                    pct, fl, band_text = default_band(median, unit)
                limit = max(abs(median) * pct / 100.0, fl)
                hit = abs(v - median) > limit
            if hit:
                ratio = v / median if median else None
                dev = f"{f'{ratio:.2g}' if ratio < 100 else f'{ratio:.0f}'}× median" if ratio is not None else f"{_fmt(v)}{u} against a median of 0"
                txt = f"≠ batch: {dev} (median {_fmt(median)}{u}, band {band_text})"
                flagged[b] = {"value": v, "kind": "band", "ratio": ratio, "text": txt}
    return {"median": median, "unit": ud, "band": band_text, "n": len(nums), "n_over": len(over), "flagged": flagged}


def similarity_all(state) -> dict:
    """{test id: similarity(...)} for every number test with enough accepted readings in the current attempt. Read only."""
    out = {}
    for s in state.proto.data["steps"]:
        for sec in s.get("sections", []):
            for t in sec.get("tests", []):
                if t.get("kind") != "number":
                    continue
                col = {}
                for b in state.boards:
                    bl = b["label"]
                    if not sec_applies(state, s, sec, bl):
                        continue
                    c = state.cells.get((bl, t["id"], state.attempt(bl, s["id"])))
                    if c and c.get("status") == "accepted" and c.get("value") is not None:
                        col[bl] = c["value"]
                r = similarity(t, col)
                if r:
                    out[t["id"]] = r
    return out


def eval_gate(state, idx: int) -> dict:
    """Per-board verdict of the plan's decision gate number `idx`. The app proposes; the operator decides."""
    gates = state.proto.data.get("tier_gates") or []
    gt = gates[idx]
    rules = {r["type"]: r for r in gt["rules"]}
    scope = list(_scope_tests(state, gt["after"]))
    steps_in_scope = {s["id"] for s, _sec, _t in scope}
    to = gt.get("to") or {"tier": 2, "mode": ""}
    labels = [b["label"] for b in state.boards]
    targets = tier2_targets(state, to)
    min_boards = (rules.get("outlier") or {}).get("min_boards", 5)
    readings: dict = {}      # test id -> {board: number}
    for s, sec, t in scope:
        for b in labels:
            if not state.applies(s, b):
                continue
            c = state.cells.get((b, t["id"], state.attempt(b, s["id"])))
            if c and c.get("status") == "accepted":
                x = _num(c.get("value"))
                if x is not None:
                    readings.setdefault(t["id"], {})[b] = x
    out_boards, oob_count, tested = [], {}, {}
    for b in labels:
        reasons, outliers = [], []
        total = settled = 0
        for s, sec, t in scope:
            if not state.applies(s, b):
                continue
            a = state.attempt(b, s["id"])
            c = state.cells.get((b, t["id"], a))
            total += 1
            if c and c.get("status") in ("accepted", "skipped"):
                settled += 1
            if c and c.get("status") == "accepted":
                tested[t["id"]] = tested.get(t["id"], 0) + 1
            if "out_of_band" in rules and out_of_band(c):
                oob_count[t["id"]] = oob_count.get(t["id"], 0) + 1
                reasons.append(f"{t['id']} {c.get('value')} {c.get('unit') or ''} out of band".replace("  ", " ") +
                               (f" ({c['reason']})" if c.get("reason") else ""))
            if "stop" in rules:
                for st in state.stops:
                    if st["board"] == b and st["test_id"] == t["id"] and st.get("attempt") == a:
                        reasons.append(f"{t['id']} stop rule tripped: {st['text']}")
            if "outlier" in rules and c and c.get("status") == "accepted" and t.get("outlier"):
                col = readings.get(t["id"], {})
                if len(col) >= min_boards:
                    hit, _m, text = outlier_check(t, c.get("value"), list(col.values()))
                    if hit:
                        outliers.append(text)
        assigned = any(set(ids) & assigned_sections(state, sid, b) for sid, ids in targets.items())
        rej = next((r for r in state.gate_rejects if r["gate"] == idx and r["board"] == b
                    and r.get("attempts") == {sid: state.attempt(b, sid) for sid in steps_in_scope}), None)
        complete = total > 0 and settled == total
        if reasons:
            verdict = "tier2"
        elif outliers and complete:
            verdict = "tier2" if gt["after"].get("mode") != "unpowered" else "outliers"
            if verdict == "tier2":
                reasons = outliers
        elif complete:
            verdict = "clean"
        else:
            verdict = "waiting"
        out_boards.append({"board": b, "verdict": verdict, "reasons": reasons, "outliers": outliers, "settled": settled, "total": total,
                           "complete": complete, "assigned": assigned, "rejected": rej["reason"] if rej else None})
    halt = []
    if "halt" in rules:
        pct = rules["halt"]["pct"]
        for tid, n in oob_count.items():
            if tested.get(tid, 0) >= 3 and n / tested[tid] * 100.0 > pct:
                halt.append(f"{tid} is out of band on {n} of {tested[tid]} boards: stop the batch and tell the project lead before reworking anything")
    return {"index": idx, "after": gt["after"], "to": to, "rules": [r["type"] for r in gt["rules"]], "boards": out_boards, "halt": halt,
            "hold_power": "hold_power" in rules, "title": gate_title(gt)}


def gate_title(gt) -> str:
    a = gt["after"] or {}
    t = f"Decision gate after Tier {a.get('tier')}"
    if a.get("mode"):
        t += f" {a['mode']}"
    return t


def tier2_targets(state, gt_to: dict) -> dict:
    """{step id: [Tier 2 section ids]} a gate sends a board to."""
    out = {}
    for s in state.proto.data["steps"]:
        if gt_to.get("mode") and s["id"] != gt_to["mode"]:
            continue
        ids = [sec["id"] for sec in s.get("sections", []) if is_conditional(sec) and tier_of(sec) == gt_to["tier"]]
        if ids:
            out[s["id"]] = ids
    return out


def hold_reason(state, board: str):
    """The power interlock. Applies only when the plan's gate has a `hold power` rule. A board with an out-of-band or stopped Tier 1
    unpowered reading may not be powered until (1) its Tier 2 sections are settled and (2) Tier 1 unpowered has been repeated and settled."""
    gates = state.proto.data.get("tier_gates") or []
    if not any(r["type"] == "hold_power" for g in gates for r in g["rules"]):
        return None
    un = state.proto.step("unpowered")
    if not un or not state.applies(un, board):
        return None
    cur = state.attempt(board, "unpowered")
    t1 = [(sec, t) for sec in un.get("sections", []) if not is_conditional(sec) for t in sec.get("tests", [])]

    def bad_in(att):
        hits = []
        for _sec, t in t1:
            c = state.cells.get((board, t["id"], att))
            # a value superseded by a re-measure (an action ran again, or Redo) still counts: only a real step retest repeats Tier 1
            if out_of_band(c) or any(h.get("remeasured") and h.get("verdict") in ("fail", "review") for h in (c or {}).get("history") or []):
                hits.append(t["id"])
        for st in state.stops:
            if st["board"] == board and st.get("step") == "unpowered" and st.get("attempt") == att and st["test_id"] not in hits:
                hits.append(st["test_id"])
        return hits
    bad_att = next((a for a in range(cur, 0, -1) if bad_in(a)), None)
    if bad_att is None:
        return None
    ids = ", ".join(bad_in(bad_att))
    rec = state.assigns.get((board, "unpowered"))
    secs = [s for s in un.get("sections", []) if is_conditional(s)]
    if rec and rec["attempt"] == bad_att and rec["sections"]:
        todo = [t for s in secs if s["id"] in rec["sections"] for t in s.get("tests", [])
                if (state.cells.get((board, t["id"], bad_att)) or {}).get("status") not in ("accepted", "skipped")]
        if todo:
            return f"{board} had an out-of-band unpowered reading ({ids}); its Tier 2 is not settled ({len(todo)} test(s) open)"
    else:
        return f"{board} had an out-of-band unpowered reading ({ids}); send it to Tier 2 (Decision gate) and settle that before powering"
    if cur == bad_att:
        return f"{board} had an out-of-band unpowered reading ({ids}); repeat Tier 1 unpowered (open a retest of Unpowered testing) after the rework"
    open_ = [t for _s, t in t1 if (state.cells.get((board, t["id"], cur)) or {}).get("status") not in ("accepted", "skipped")]
    if open_:
        return f"{board}: the repeat of Tier 1 unpowered is not finished ({len(open_)} test(s) open)"
    return None
