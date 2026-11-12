"""The Results explorer's data (PW-3): what the instrument actually measured for one board in one step.

Built from what is already on disk and in the journal: the run folders under runs/ (measurements.json written when an action reports
its analysis), the proposals the actions made, and the derived answers. Read only; nothing here records a result. A row names the test
it was proposed to (`test`); a sample row is offered as an override ("Use this value") only when its unit is the test's unit.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path


def _iso(ts: str, add_s: float = 0.0) -> str:
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return (d + timedelta(seconds=add_s)).isoformat(timespec="milliseconds")
    except ValueError:
        return str(ts)


def _digest(an: dict) -> str:
    """One line of the analysis block: its yes/no facts and its final value, for the explorer."""
    bits = []
    for k in ("tripped", "within_limit", "cc_seen", "at_limit", "settled", "complete", "rail_collapsed"):
        if isinstance(an.get(k), bool):
            bits.append(f"{k.replace('_', ' ')} {'yes' if an[k] else 'no'}")
    for k in ("current", "voltage", "rail"):
        if isinstance(an.get(k), dict) and "final" in an[k]:
            bits.append(f"{k} min {an[k].get('min'):.5g} max {an[k].get('max'):.5g} final {an[k]['final']:.5g}{(' ' + an['current_unit']) if k == 'current' and an.get('current_unit') else ''}")
    if an.get("time_at_limit_s") is not None:
        bits.append(f"time at limit {an['time_at_limit_s']} s")
    if an.get("stopped_because"):
        bits.append("stopped: " + str(an["stopped_because"])[:80])
    return "; ".join(bits)


def collect(engine, batch_dir: Path, board: str, step_id: str) -> dict:
    proto = engine.proto
    st = proto.step(step_id)
    if st is None:
        return {"rows": [], "runs": []}
    tests = {t["id"]: t for sec in st.get("sections", []) for t in sec.get("tests", []) if isinstance(t, dict) and "id" in t}
    acts = {a["id"]: a for sec in st.get("sections", []) for a in sec.get("actions", []) if isinstance(a, dict) and "id" in a}
    runs: dict[int, dict] = {}
    for ev in engine.journal.events:
        if ev["type"] == "action_start":
            p = ev["payload"]
            if p.get("board") == board and p.get("step") == step_id:
                runs[p["run"]] = {"run": p["run"], "action": p.get("action"), "plugin": p.get("plugin"), "ts": ev["ts"],
                                  "workdir": p.get("workdir"), "title": (acts.get(p.get("action")) or {}).get("title", p.get("action"))}
    rows: list[dict] = []
    seen_derived: set[tuple] = set()
    for run, r in sorted(runs.items()):
        a = acts.get(r["action"]) or {}
        mp = (a.get("params") or {}).get("map") or {}
        label = f"run {run}"
        try:
            m = json.loads((Path(r["workdir"]) / "measurements.json").read_text())
        except (OSError, ValueError, TypeError):
            m = None
        if not m:
            continue
        for s in m.get("samples") or []:
            tid = mp.get(s.get("map_key")) if s.get("map_key") else s.get("test")
            t = tests.get(tid)
            ok = bool(t) and t.get("kind") == "number" and (t.get("unit") or "") == (s.get("unit") or "")
            rows.append({"kind": "sample", "ts": _iso(r["ts"], float(s.get("t_s") or 0)), "run": run, "run_label": label, "action": r["title"],
                         "channel": s.get("channel"), "quantity": s.get("quantity"), "value": s.get("value"), "unit": s.get("unit"),
                         "mode": s.get("mode"), "test": tid if ok else None})
        for d in m.get("derived") or []:
            seen_derived.add((run, d["test"]))
            t = tests.get(d["test"]) or {}
            rows.append({"kind": "derived", "ts": _iso(r["ts"]), "run": run, "run_label": label, "action": r["title"], "channel": None,
                         "quantity": t.get("title", d["test"]), "value": d["value"], "unit": d.get("unit"), "mode": None, "test": d["test"],
                         "evidence": f"derived from {d['source_path']} = {d['source_value']}" + (f" ({d['note']})" if d.get("note") else "")})
        an = m.get("analysis") or {}
        if an:
            rows.append({"kind": "analysis", "ts": _iso(r["ts"]), "run": run, "run_label": label, "action": r["title"], "channel": None,
                         "quantity": "analysis", "value": _digest(an), "unit": None, "mode": None, "test": None})
    for ev in engine.journal.events:
        if ev["type"] != "proposal":
            continue
        p = ev["payload"]
        src = p.get("source") or {}
        if p.get("board") != board or p.get("test_id") not in tests or src.get("kind") != "action":
            continue
        run = src.get("run_ref")
        if (run, p["test_id"]) in seen_derived:
            continue
        t = tests[p["test_id"]]
        val = p.get("value")
        if isinstance(val, dict):
            val = f"table, {len(val.get('rows', []))} rows"
        rr = runs.get(run) or {}
        rows.append({"kind": "proposal", "ts": ev["ts"], "run": run, "run_label": f"run {run}", "action": rr.get("title") or src.get("component"),
                     "channel": None, "quantity": t.get("title", p["test_id"]), "value": val, "unit": p.get("unit"), "mode": None,
                     "test": p["test_id"] if t.get("kind") != "table" else None, "evidence": p.get("raw")})
    rows.sort(key=lambda x: (x["ts"], x["run"] or 0))
    for i, r in enumerate(rows):
        r["i"] = i
    return {"rows": rows, "runs": list(runs.values()), "tests": {k: {"unit": v.get("unit"), "title": v.get("title"), "kind": v.get("kind")} for k, v in tests.items()}}
