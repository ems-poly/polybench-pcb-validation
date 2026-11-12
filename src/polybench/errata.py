"""Errata candidates for the next board revision: a pure function of the journal state.

Three sources, nothing else:
  1. every operator note of category 'design issue' (current version of the note);
  2. every accepted FAIL (not a retest that passed) that carries a note of another category;
  3. every AOI candidate the operator confirmed as a defect.
The output is a markdown table with status 'candidate'. It is written to the batch folder only. Merging into the programme's living
register is done by the project lead or an agent after the engineer has reviewed the candidates; this module never touches that register.
"""
from __future__ import annotations

from .aoi import link
from .engine import State, expected_text
from .protocol import design_label

COLUMNS = ["ID", "Origin", "Observation", "Suggested fix", "Evidence", "Status"]


def _origin(step, boards, designators, ts):
    parts = [f"step: {step or 'none'}", f"board: {', '.join(boards) if boards else 'none'}"]
    if designators:
        parts.append("designators: " + ", ".join(designators))
    parts.append("time: " + ts)
    return "; ".join(parts)


def _step_title(st: State, sid: str) -> str:
    s = next((x for x in st.proto.data["steps"] if x["id"] == sid), None)
    return s.get("title", sid) if s else sid


def collect(st: State) -> list[dict]:
    rows: list[dict] = []
    run = st.aoi_current_run()
    items = {i["key"]: i for i in st.aoi_all_items(run)}

    # 1. design issue notes
    for n in st.notes:
        if n["category"] != "design issue":
            continue
        ev = [f"journal event {n['id']}" + (f" (edited, event {n['edits'][-1]})" if n["edits"] else "") + " in ../journal.jsonl"]
        if n.get("aoi_key") and n["aoi_key"] in items:
            ev.append("AOI " + n["aoi_key"])
        if n.get("scan"):
            ev.append("scan " + n["scan"])
        rows.append({"id": f"N{n['id']}", "kind": "note", "step": _step_title(st, n["step"]), "boards": n["boards"],
                     "designators": n["designators"], "ts": n["edited_ts"] if n["edits"] else n["ts"], "observation": n["text"],
                     "fix": n["suggested_fix"], "evidence": "; ".join(ev), "sort": (0, n["id"])})

    # 2. accepted FAILs that carry a note of another category, one row per test
    by_test: dict[str, list[dict]] = {}
    for n in st.notes:
        if n["category"] != "design issue" and n["test_id"]:
            by_test.setdefault(n["test_id"], []).append(n)
    for tid, notes in by_test.items():
        fails = [(b["label"], st.cell(b["label"], tid)) for b in st.boards]
        fails = [(b, c) for b, c in fails if c["status"] == "accepted" and c.get("verdict") == "fail"]
        # a note on a board other than the failing one still belongs to the test when it names no board
        fails = [(b, c) for b, c in fails if any(not n["boards"] or b in n["boards"] for n in notes)]
        if not fails:
            continue
        _, _, t = st.test_index[tid]
        res = "; ".join(f"{b} = {c['value']}{(' ' + c['unit']) if c.get('unit') else ''}" for b, c in fails)
        text = "; ".join(n["text"] for n in notes if not n["boards"] or set(n["boards"]) & {b for b, _ in fails})
        evid = [f"journal event {n['id']} in ../journal.jsonl" for n in notes]
        evid += sorted({c["evidence"] for _, c in fails if c.get("evidence")})
        rows.append({"id": f"F-{tid}", "kind": "fail", "step": _step_title(st, st.test_index[tid][0]["id"]),
                     "boards": [b for b, _ in fails], "designators": sorted({d for n in notes for d in n["designators"]}),
                     "ts": max(c["ts"] for _, c in fails), "observation": f"{tid} {t['title']}: FAIL ({res}), expected {expected_text(t)}. {text}",
                     "fix": "", "evidence": "; ".join(evid), "sort": (1, tid)})

    # 3. AOI candidates confirmed as defects
    for key, d in sorted(st.aoi_decisions.items()):
        if d["decision"] != "confirmed":
            continue
        a = d.get("aoi") or {}
        it = items.get(key, {})
        if it.get("manual"):
            where = ", ".join(it["designators"]) or "no designator"
            rows.append({"id": f"A-{d['board']}-{d['id']}", "kind": "aoi", "step": "Visual inspection (AOI missed)", "boards": [d["board"]],
                         "designators": list(it["designators"]), "ts": d["decided_at"],
                         "observation": f"AOI missed: operator-found {it['manual_kind']} at {where} (found by {d.get('operator') or 'operator'}, "
                                        f"box {', '.join(f'{v:.2f}' for v in it['box_mm'])} mm)" + (f". {d['note']}" if d.get("note") else ""),
                         "fix": "", "evidence": f"journal event {it['event']} in ../journal.jsonl; aoi/labels.jsonl", "sort": (2, key)})
            continue
        ev = ([it["evidence"][0]] if it.get("evidence") else []) + ([f"AOI run {d['run']}"] if d.get("run") else [])
        rows.append({"id": f"A-{d['board']}-{d['id']}", "kind": "aoi", "step": "Visual inspection (AOI)", "boards": [d["board"]],
                     "designators": link.designators_of(str(d["id"])),
                     "ts": d["decided_at"], "observation": f"AOI {a.get('kind', '')} {d['id']}: {a.get('value', '')} (confirmed by {d.get('operator') or 'operator'})"
                     + (f". {d['note']}" if d.get("note") else ""), "fix": "", "evidence": "; ".join(ev), "sort": (2, key)})
    rows.sort(key=lambda r: r["sort"])
    return rows


def _cell(x) -> str:
    return str(x).replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def to_markdown(st: State, rows: list[dict], batch: str, journal_hash: str, generated: str) -> str:
    d = st.proto.data["design"]
    out = [f"# Errata candidates: {design_label(d)}, batch {batch}", "",
           f"- **Generated:** {generated}",
           f"- **Journal:** `journal.jsonl` in the batch folder, head hash `{journal_hash[:16]}`",
           f"- **Data origin:** {'SIMULATED, nothing here was measured on a board' if st.meta.get('simulate') else 'entered or measured at the bench'}",
           f"- **Candidates:** {len(rows)} ({sum(r['kind'] == 'note' for r in rows)} design-issue note(s), "
           f"{sum(r['kind'] == 'fail' for r in rows)} FAILs with notes, {sum(r['kind'] == 'aoi' for r in rows)} AOI-confirmed defects, of which {sum(r['kind'] == 'aoi' and r['step'].endswith('(AOI missed)') for r in rows)} AOI missed)",
           "", "These are candidates, not register entries. They are not merged into any programme register. The project lead or an agent merges them into "
           "the living register after the engineer has reviewed them. Evidence paths are relative to the batch folder.", ""]
    out.append("| " + " | ".join(COLUMNS) + " |")
    out.append("|" + "---|" * len(COLUMNS))
    for r in rows:
        out.append("| " + " | ".join(_cell(x) for x in (r["id"], _origin(r["step"], r["boards"], r["designators"], r["ts"]),
                                                          r["observation"], r["fix"] or "none given", r["evidence"] or "none", "candidate")) + " |")
    if not rows:
        out.append("| none | | | | | |")
    out.append("")
    return "\n".join(out)
