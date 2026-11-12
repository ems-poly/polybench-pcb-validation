"""Report: a pure function of (protocol, journal). Markdown and self-contained HTML from one model."""
from __future__ import annotations

import html

from .engine import State, expected_text, source_text
from .protocol import design_label


def _val(c):
    if not c or c["status"] == "empty":
        return ""
    if c["status"] == "skipped":
        return "skipped"
    if c.get("origin") == "operator_confirmed":
        return "confirmed in range, no reading"
    if isinstance(c["value"], dict):
        return f"table, {len(c['value']['rows'])} rows"
    return f"{c['value']}" + (f" {c['unit']}" if c.get("unit") else "")


def _mark(c):
    v = c.get("verdict")
    return {"fail": " FAIL", "review": " REVIEW"}.get(v, "")


def build_model(st: State, journal_hash: str = "") -> dict:
    proto = st.proto.data
    snap = st.snapshot()
    blocks = []
    simulated = st.meta.get("simulate", False)
    boards = [b["label"] for b in st.boards]
    d = proto["design"]
    title = f"Validation report: {design_label(d)}, batch {st.meta.get('name', '')}"
    if simulated:
        blocks.append(("banner", "SIMULATED DATA: this batch ran with --simulate. Nothing here was measured on a board."))

    # step readiness
    steps = [s for s in snap["steps"] if s["kind"] not in ("setup", "report", "project")]
    ready = {}
    for s in steps:
        has = any(s_b["settled"] for s_b in s["per_board"].values())
        sim_here = simulated or _step_has_sim(st, s)
        ready[s["id"]] = "UNVALIDATED" if not has else ("HOST-ONLY" if sim_here else "HARDWARE-VALIDATED")
    if not any(v != "UNVALIDATED" for v in ready.values()):
        overall = "UNVALIDATED"
    elif any(v == "HOST-ONLY" for v in ready.values()):
        overall = "HOST-ONLY"
    elif any(v == "UNVALIDATED" for v in ready.values()):
        overall = "PARTIAL (steps without results are UNVALIDATED)"
    else:
        overall = "HARDWARE-VALIDATED"
    blocks.append(("kv", [
        ("Design", f"{design_label(d)}, plan revision {d.get('plan_revision', '')}"),
        ("Batch", f"{st.meta.get('name', '')} ({len(boards)} boards, created {st.meta.get('created', '')})"),
        ("Protocol hash", st.meta.get("protocol_hash", "")),
        ("Operator", st.meta.get("operator", "") or "not recorded"),
        ("Data origin", "SIMULATED" if simulated else "entered or measured at the bench"),
        ("Readiness", overall),
        ("Last event", st.last_ts),
    ]))
    blocks.append(("table", ["Board", "Serial"], [[b["label"], b["serial"] or ""] for b in st.boards], None))
    pj = snap.get("project") or {}
    if pj.get("select") or pj.get("ecad_skipped"):
        sel, cc, acc, sk = pj.get("select"), pj.get("crosscheck"), pj.get("accepted"), pj.get("ecad_skipped")
        rows_ = []
        if sel:
            rows_ += [("ECAD document", f"{sel.get('document')} ({sel.get('editor')}, {sel.get('elements')} parts, fingerprint {sel.get('fingerprint')})"),
                      ("ECAD program", f"{sel.get('program')} {sel.get('version', '')}".strip() + (" (simulated)" if sel.get("simulated") else ""))]
        if cc:
            rows_.append(("BOM cross-check", f"{cc.get('matched')} matched, {len(cc.get('missing_in_design', []))} missing in design, "
                                             f"{len(cc.get('missing_in_bom', []))} missing in BOM, {len(cc.get('dnf', []))} DNF"))
        if acc:
            rows_.append(("Cross-check review", f"reviewed by {acc.get('operator') or 'unnamed'}" + (f": {acc['note']}" if acc.get("note") else "")))
        if sk:
            rows_.append(("ECAD", f"continued WITHOUT ECAD: {sk.get('reason')}"))
        blocks.append(("kv", rows_))

    # summary matrix
    head = ["Board"] + [s["title"] for s in steps]
    rows, cls = [], []
    for b in boards:
        row, crow = [b], [""]
        for s in steps:
            sb = s["per_board"][b]
            if sb["state"] == "na":
                row.append("n/a")
                crow.append("na")
                continue
            if sb["settled"] == 0 and not sb.get("pending"):
                row.append("")
                crow.append("")
                continue
            att = sb.get("attempt", 1)
            if sb["settled"] < sb["applicable"]:
                row.append(f"partial {sb['settled']}/{sb['applicable']}")
                crow.append("part")
            elif sb.get("fails"):
                row.append("FAIL" + (f" (attempt {att})" if att > 1 else ""))
                crow.append("fail")
            elif sb.get("reviews"):
                row.append("REVIEW")
                crow.append("review")
            elif att > 1:
                row.append("PASS after retest")
                crow.append("retest")
            else:
                row.append("PASS")
                crow.append("pass")
        rows.append(row)
        cls.append(crow)
    blocks.append(("h2", "Summary"))
    blocks.append(("table", head, rows, cls))
    blocks.append(("p", "Blank means not run. PASS means every applicable test is settled and none was accepted as FAIL or REVIEW."))
    blocks.append(("table", ["Step", "Readiness"], [[s["title"], ready[s["id"]]] for s in steps], None))

    # results by step, untested omitted
    blocks.append(("h2", "Results by step"))
    for s in steps:
        if s["kind"] == "assembly":
            lines = []
            for b in boards:
                placed = set(st.placed.get(b, set()))
                if placed:
                    des = st.bom_designators()
                    lines.append(f"{b}: {len(placed & set(des))} of {len(des)} designators placed")
            if lines:
                blocks.append(("h3", s["title"]))
                blocks.append(("ul", lines))
            continue
        trows, tcls = [], []
        used_boards = [b for b in boards if any(
            (snap["cells"].get(f"{b}|{t['id']}") or {}).get("status") in ("accepted", "skipped")
            for sec in s["sections"] for t in sec["tests"])]
        for sec in s["sections"]:
            for t in sec["tests"]:
                cs = [snap["cells"].get(f"{b}|{t['id']}") for b in used_boards]
                if not any(c and c["status"] in ("accepted", "skipped") for c in cs):
                    continue  # untested: omitted
                row, crow = [t["id"], t["title"], t["expected"]], ["", "", ""]
                for c in cs:
                    txt = _val(c) + (_mark(c) if c else "")
                    if c and c.get("attempt", 1) > 1:
                        txt += f" (R{c['attempt']})"
                    row.append(txt)
                    v = (c or {}).get("verdict")
                    crow.append({"fail": "fail", "review": "review", "pass": "pass"}.get(v, "") if c and c["status"] == "accepted" else ("na" if c else ""))
                trows.append(row)
                tcls.append(crow)
        if trows:
            blocks.append(("h3", s["title"]))
            blocks.append(("table", ["Test", "Title", "Expected"] + used_boards, trows, tcls))

    # table results: one table per recorded cell, each row with its verdict
    for key, c in snap["cells"].items():
        if c["status"] != "accepted" or not isinstance(c.get("value"), dict):
            continue
        b, tid = key.split("|")
        t = st.test_index[tid][2]
        cols = c["value"]["columns"]
        head = [next((x.get("title") or x["id"] for x in t.get("columns", []) if x["id"] == cid), cid) +
                (f" ({x['unit']})" if (x := next((y for y in t.get("columns", []) if y["id"] == cid), {})).get("unit") else "") for cid in cols] + ["Verdict"]
        rv = c.get("row_verdicts") or [{"verdict": "none"}] * len(c["value"]["rows"])
        rows = [[(f"{v:g}" if isinstance(v, float) else str(v)) for v in r] + [(rv[i]["verdict"] if rv[i]["verdict"] != "none" else "").upper()]
                for i, r in enumerate(c["value"]["rows"])]
        cls = [[""] * len(cols) + [{"fail": "fail", "review": "review", "pass": "pass"}.get(rv[i]["verdict"], "")] for i in range(len(rows))]
        src = (c.get("source") or {})
        blocks.append(("h3", f"{b} {tid} {t.get('title', '')}" + (f" (attempt {c['attempt']})" if c.get("attempt", 1) > 1 else "")))
        blocks.append(("p", f"{_mark(c).strip() or 'recorded'}{' - ' + c['reason'] if c.get('reason') else ''}; source {src.get('component', src.get('kind', ''))}"
                            + (f", attachment {c['value']['attachment']}" if c["value"].get("attachment") else "")))
        blocks.append(("table", head, rows, cls))

    # range confirmations: the operator said the reading is inside the limit, no number was given
    conf = [f"{k.split('|')[0]} {k.split('|')[1]} ({st.test_index[k.split('|')[1]][2].get('title', '')}): inside {expected_text(st.test_index[k.split('|')[1]][2])}"
            for k, c in snap["cells"].items() if c["status"] == "accepted" and c.get("origin") == "operator_confirmed" and k.split("|")[1] in st.test_index]
    if conf:
        blocks.append(("h2", "Confirmed in range, no reading"))
        blocks.append(("p", "The operator confirmed these readings by voice as inside the limit. No number was given, so they take no part in medians or comparisons."))
        blocks.append(("ul", conf))

    # retests
    blocks.append(("h2", "Retests"))
    if not st.retests:
        blocks.append(("p", "No retests."))
    for r in st.retests:
        s = st.proto.step(r["step"])
        first, now = [], []
        for sec in s.get("sections", []):
            for t in sec.get("tests", []):
                c1 = st.cells.get((r["board"], t["id"], r["attempt"] - 1))
                c2 = st.cells.get((r["board"], t["id"], r["attempt"]))
                if c1 and c1["status"] == "accepted" and c1.get("verdict") in ("fail", "review"):
                    first.append(f"{t['id']} {_val(c1)} ({c1['verdict'].upper()})")
                    now.append(f"{t['id']} {_val(c2) or 'not yet retested'}" + (f" ({c2['verdict'].upper()})" if c2 and c2.get("verdict") in ("fail", "review", "pass") else ""))
        blocks.append(("h3", f"{r['board']} / {s['title']} / attempt {r['attempt']}"))
        blocks.append(("kv", [("First result", "; ".join(first) or "no failing result recorded in the earlier attempt"),
                              ("Cause", r.get("cause", "")), ("Fix", r.get("fix", "") or "not recorded"),
                              ("Retest result", "; ".join(now) or "not yet retested"), ("When", r["ts"])]))

    # open items
    items = []
    for key, c in snap["cells"].items():
        b, tid = key.split("|")
        if c["status"] == "accepted":
            if c["verdict"] == "review":
                items.append(f"{b} {tid}: guess limit exceeded ({_val(c)}), review pending")
            if any(f.startswith("outside_tbr") for f in c.get("flags", [])):
                items.append(f"{b} {tid}: reading {_val(c)} is outside a TBR limit ({expected_text(st.test_index[tid][2])})")
            if c["verdict"] == "fail":
                items.append(f"{b} {tid}: FAIL accepted ({_val(c)}, expected {expected_text(st.test_index[tid][2])})")
        elif c["status"] == "skipped":
            items.append(f"{b} {tid}: skipped, reason: {c['skip_reason']}")
        for p in c.get("proposals", []):
            items.append(f"{b} {tid}: proposal {p['value']} from {p['source'].get('component')} not yet accepted or rejected")
    for s, sec, t in st.proto.tests():
        lim = t.get("limit") or {}
        if lim.get("class") in ("tbr", "guess"):
            items.append(f"{t['id']} limit is {lim['class'].upper()}, closes on: {lim.get('closes_on')}")
    for o in snap["overrides"]:
        items.append(f"Gate override {o['board']} / {o['step']}: {o['reason']}")
    for s_ in st.stops:
        items.append(f"Stop on {s_['board']} {s_['test_id']} ({s_['text']}): " + (f"acknowledged, {s_['ack']['reason']}" if s_["ack"] else "NOT acknowledged"))
    for s in snap["steps"]:
        if s["kind"] in ("setup", "report", "project"):
            continue
        for b in boards:
            sb = s["per_board"][b]
            if sb["state"] != "na" and 0 < sb["settled"] < sb["applicable"]:
                items.append(f"{b} {s['title']}: {sb['applicable'] - sb['settled']} tests not settled")
    cap_missing = [c["name"] for c in proto["capabilities"] if c.get("optional")]
    if cap_missing and simulated:
        pass
    for n in st.notes:
        if n["category"] == "question":
            items.append(f"Question (note {n['id']}, {_where(n)}): {n['text']}")
    aoi = snap.get("aoi")
    if aoi and aoi.get("run"):
        run, cnt = aoi["run"], aoi["counts"]
        blocks.append(("h2", "Automated optical inspection (AOI)" + (" - EXPERIMENTAL diff mode" if run.get("mode") == "diff" else "")))
        if run.get("mode") == "diff":
            blocks.append(("banner", f"EXPERIMENTAL, HOST-ONLY, UNVALIDATED: layout-free diff mode. Every candidate is a region that differs from the reference board "
                                     f"{run.get('reference')}; two photos cannot say which board is wrong, so each confirmed issue names the board the operator chose."))
        blocks.append(("p", "AOI is review assistance: it lists candidates and the operator decides. A board is never certified good by AOI, "
                            "and a candidate that nobody looked at is open."))
        if run.get("synthetic"):
            blocks.append(("banner", "The AOI result below is SIMULATED: no photo was analysed."))
        blocks.append(("kv", [("Run", f"{run['run']} at {run['ts']}"), ("Photos analysed", ", ".join(f"{x['board']} {x['side']}: {x['name']}" for x in run.get("photos", [])) or "none"),
                              ("Candidates", f"{cnt['total']} (confirmed {cnt['confirmed']}, not a defect {cnt['rejected']}, ignored {cnt['unsure']}, undecided {cnt['total'] - cnt['decided']})")]
                             + ([("Operator-found (AOI missed)", str(aoi["manual"]))] if aoi.get("manual") else [])))
        rows = []
        for b in boards:
            pb = aoi["per_board"].get(b)
            if pb:
                rows.append([b, pb["total"], pb["confirmed"], pb["rejected"], pb["unsure"], pb["undecided"]])
        if rows:
            blocks.append(("table", ["Board", "Candidates", "Confirmed", "Not a defect", "Ignored", "Undecided"], rows, None))
        issues = []
        for it in st.aoi_all_items():
            d = aoi["decisions"].get(it["key"])
            issues.append([it["board"], (f"{it['id']} ({', '.join(it['designators']) or 'no designator'})" if it.get("manual") else it["id"]),
                           "AOI MISSED, " + it["manual_kind"] if it.get("manual") else it["kind"], "operator-found" if it.get("manual") else it.get("value", ""),
                           ((("defect, fixed" if it["key"] in st.aoi_reworked else "confirmed") if d["decision"] == "confirmed" else "ignored" if d["decision"] == "unsure" else d["decision"]) if d else "undecided") + (f" by {d['operator']}" if d and d.get("operator") else ""),
                           ", ".join(d["tests"]) if d and d["decision"] == "confirmed" else "", (d or {}).get("note", "")])
        if issues:
            blocks.append(("h3", "Issue list"))
            blocks.append(("table", ["Board", "Issue", "Kind", "AOI value", "Operator decision", "Visual checks failed", "Operator note"], issues,
                           [["", "", "", "", "fail" if r[4].startswith("confirmed") else "review" if r[4].startswith("defect") else "review" if r[4].startswith(("ignored", "undecided")) else "pass" if r[4].startswith("rejected") else "", "", ""] for r in issues]))
        for it in st.aoi_all_items():
            d = aoi["decisions"].get(it["key"])
            if not d or d["decision"] == "unsure":
                items.append(f"{it['board']} AOI {it['id']}: " + ("WARNING ignored by the operator (does not block Unpowered)" if d else "not reviewed"))
            elif d["decision"] == "confirmed" and it["key"] not in st.aoi_reworked:
                items.append(f"{it['board']} AOI {it['id']}: confirmed defect, not yet reworked (blocks Unpowered)")
    blocks += _notes_blocks(st)
    blocks.append(("h2", "Open items"))
    blocks.append(("ul", items) if items else ("p", "None."))

    # evidence
    ev = [f"run {a['run']} {a['plugin']} on {a['board']}: {a['state']}" + (f" ({a['text']})" if a.get("text") else "")
          for a in st.actions if a.get("board")]
    blocks.append(("h2", "Actions and evidence"))
    blocks.append(("ul", ev) if ev else ("p", "No actions were run."))
    blocks.append(("p", "Evidence files are in the batch folder under runs/ (not copied into this report)."))
    return {"title": title, "blocks": blocks, "simulated": simulated}


def _where(n) -> str:
    bits = [", ".join(n["boards"]) or "no board"]
    if n["test_id"]:
        bits.append(n["test_id"])
    if n["designators"]:
        bits.append(" ".join(n["designators"]))
    return " ".join(bits)


def _notes_blocks(st: State) -> list:
    """Operator notes and comments, grouped by step in protocol order. Each line carries its links and its category."""
    if not st.notes:
        return [("h2", "Notes and comments"), ("p", "No notes were recorded.")]
    out = [("h2", "Notes and comments")]
    out.append(("p", f"{len(st.notes)} note(s). Design issues feed the errata candidates of the next revision."))
    titles = [(s["id"], s.get("title", s["id"])) for s in st.proto.data["steps"]]
    order = [i for i, _ in titles]
    groups: dict[str, list] = {}
    for n in st.notes:
        groups.setdefault(n["step"] if n["step"] in order else "", []).append(n)
    for sid in order + [""]:
        ns = groups.get(sid)
        if not ns:
            continue
        out.append(("h3", dict(titles).get(sid, "No step")))
        rows = []
        for n in ns:
            cat = n["category"] + (" (edited)" if n["edits"] else "")
            txt = n["text"] + (f" Suggested fix: {n['suggested_fix']}" if n["suggested_fix"] else "")
            rows.append([n["id"], n["ts"][:16].replace("T", " "), cat, _where(n), txt])
        out.append(("table", ["Note", "Time", "Category", "Linked to", "Text"], rows,
                    [["", "", "fail" if n["category"] == "design issue" else "review" if n["category"] == "question" else "", "", ""] for n in ns]))
    return out


def _step_has_sim(st: State, s) -> bool:
    for sec in s.get("sections", []):
        for t in sec.get("tests", []):
            for b in st.boards:
                c = st.cell(b["label"], t["id"])
                if c["status"] == "accepted" and c.get("origin") == "simulated":
                    return True
    return False


def to_markdown(m: dict) -> str:
    out = [f"# {m['title']}", ""]
    for blk in m["blocks"]:
        k = blk[0]
        if k == "banner":
            out += [f"> **{blk[1]}**", ""]
        elif k == "h2":
            out += [f"## {blk[1]}", ""]
        elif k == "h3":
            out += [f"### {blk[1]}", ""]
        elif k == "p":
            out += [blk[1], ""]
        elif k == "ul":
            out += [f"- {x}" for x in blk[1]] + [""]
        elif k == "kv":
            out += [f"- **{a}:** {b}" for a, b in blk[1]] + [""]
        elif k == "table":
            _, head, rows, _c = blk
            esc = lambda x: str(x).replace("|", "\\|")
            out.append("| " + " | ".join(esc(h) for h in head) + " |")
            out.append("|" + "---|" * len(head))
            for r in rows:
                out.append("| " + " | ".join(esc(x) for x in r) + " |")
            out.append("")
    return "\n".join(out)


CSS = """
body{font:13px/1.45 -apple-system,Inter,'Segoe UI',sans-serif;margin:24px auto;max-width:1100px;padding:0 16px;color:#1b1f24;background:#fff}
h1{font-size:20px;margin:0 0 12px}h2{font-size:15px;margin:22px 0 8px;border-bottom:1px solid #d0d5db;padding-bottom:3px}h3{font-size:13px;margin:14px 0 6px}
table{border-collapse:collapse;margin:6px 0 10px}th,td{border:1px solid #d0d5db;padding:3px 8px;text-align:left;font-variant-numeric:tabular-nums}
th{background:#eef0f3}.banner{background:#fff3c4;border:1px solid #c9a100;padding:8px 12px;font-weight:600;margin:0 0 12px}
td.pass{background:#d7efdd}td.fail{background:#f4c7c3;font-weight:600}td.review{background:#ffe9a8}td.retest{background:#cfe3fb}td.part{background:#eef0f3}td.na{color:#888}
.kv b{display:inline-block;min-width:130px}ul{margin:4px 0 10px;padding-left:20px}
@media print{body{margin:0}}
"""


def to_html(m: dict) -> str:
    e = html.escape
    o = [f"<!doctype html><html><head><meta charset='utf-8'><title>{e(m['title'])}</title><style>{CSS}</style></head><body>",
         f"<h1>{e(m['title'])}</h1>"]
    for blk in m["blocks"]:
        k = blk[0]
        if k == "banner":
            o.append(f"<div class='banner'>{e(blk[1])}</div>")
        elif k in ("h2", "h3"):
            o.append(f"<{k}>{e(blk[1])}</{k}>")
        elif k == "p":
            o.append(f"<p>{e(blk[1])}</p>")
        elif k == "ul":
            o.append("<ul>" + "".join(f"<li>{e(x)}</li>" for x in blk[1]) + "</ul>")
        elif k == "kv":
            o.append("<div class='kv'>" + "".join(f"<div><b>{e(a)}</b>{e(str(b))}</div>" for a, b in blk[1]) + "</div>")
        elif k == "table":
            _, head, rows, cls = blk
            o.append("<table><tr>" + "".join(f"<th>{e(h)}</th>" for h in head) + "</tr>")
            for i, r in enumerate(rows):
                tds = []
                for j, x in enumerate(r):
                    c = cls[i][j] if cls else ""
                    tds.append(f"<td class='{c}'>{e(str(x))}</td>")
                o.append("<tr>" + "".join(tds) + "</tr>")
            o.append("</table>")
    o.append("</body></html>")
    return "\n".join(o)
