"""Test-plan co-authoring: the assistant writes a draft next to the master plan, the app lints it with the plan parser,
design step 4 shows the diff, and only the operator approves (docs/AI_CONNECTOR_ARCHITECTURE.md section 5)."""
from __future__ import annotations

import difflib
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

from .. import planfmt
from ..engine import CmdError

MAX_PLAN_BYTES = 512 * 1024
_FALLBACK_LOCK = threading.RLock()


def _lock(sess):
    """One lock per session for everything that writes the draft or the master (write, approve, reject)."""
    lk = getattr(sess, "ai_lock", None)
    if lk is None:
        with _FALLBACK_LOCK:
            lk = getattr(sess, "ai_lock", None)
            if lk is None:
                lk = sess.ai_lock = threading.RLock()
    return lk


def master_path(sess) -> Path:
    tp = (sess.proto.data.get("test_plan") or {}).get("files") or []
    if not tp:
        raise CmdError("this protocol has no test plan (add test_plan: {file: <plan.md>, generate: true} to the top protocol file)", 409)
    return Path(tp[0]["path"])


def draft_path(sess) -> Path:
    m = master_path(sess)
    return m.with_name(m.stem + ".draft.md")


def _library(sess) -> dict:
    """The protocol's action library as it is before the plan replaces any section (hand-written actions stay available)."""
    from ..protocol import load
    p = load(sess.proto.path, check_plugins=False, plan_generate=False)
    lib: dict = {}
    for s in p.data["steps"]:
        for sec in s.get("sections", []):
            for a in sec.get("actions", []) or []:
                if isinstance(a, dict) and "id" in a:
                    lib.setdefault(a["id"], a)
    return lib


def lint(sess, path: Path) -> dict:
    plan = planfmt.parse(path, sess.proto.data["bom"], set(_library(sess)))
    issues = [i.as_dict() for i in plan.issues]
    issues += revision_issues(_text(master_path(sess)), _text(path), path.name)
    return {"hash": plan.hash, "errors": sum(1 for i in issues if i["severity"] == "error"), "warnings": len(plan.warnings), "issues": issues[:200],
            "tests": sum(1 for _ in plan.tests())}


def full_check(sess, path: Path, fresh: bool = False) -> dict:
    """The draft loaded as the master plan with the full protocol loader (what startup and a plan reload run), against the current
    protocol: the actions that `propose` tests, the tier gates and every lint. Also the test IDs it adds and retires."""
    from ..protocol import load
    cache = sess.__dict__.setdefault("ai_check_cache", {})
    key = (str(path), planfmt.file_hash(_text(path)), sess.proto.hash, planfmt.file_hash(_text(master_path(sess))))
    if not fresh and key in cache:
        return cache[key]
    p = load(sess.proto.path, plan_override=path)
    cur = {t["id"] for _s, _sec, t in sess.proto.tests()}
    new = {t["id"] for _s, _sec, t in p.tests()}
    out = {"errors": len(p.errors), "warnings": len(p.warnings), "issues": [i.as_dict() for i in p.errors][:30],
           "added": sorted(new - cur), "retired": sorted(cur - new), "tests": len(new),
           "regenerates": bool((sess.proto.data.get("test_plan") or {}).get("generate"))}
    cache.clear()
    cache[key] = out
    return out


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def last_draft_event(sess) -> dict | None:
    for e in reversed(sess.journal.events):
        if e["type"] == "plan_draft":
            return e
        if e["type"] in ("plan_approved", "plan_draft_rejected"):
            return None
    return None


def status(sess, with_text: bool = True, fresh: bool = False) -> dict:
    """What step 4 and the assistant see about the draft."""
    try:
        m = master_path(sess)
    except CmdError as e:
        return {"configured": False, "master_exists": False, "draft_exists": False, "error": str(e)}
    d = draft_path(sess)
    mtext = _text(m)
    out = {"configured": True, "master": str(m), "master_name": m.name, "master_hash": planfmt.file_hash(mtext) if mtext else None,
           "draft": str(d), "draft_name": d.name, "master_exists": m.is_file(), "draft_exists": d.is_file()}
    if not d.is_file():
        return out
    dtext = _text(d)
    ev = last_draft_event(sess)
    p = (ev or {}).get("payload", {})
    lt = lint(sess, d)
    ld = full_check(sess, d, fresh) if not lt["errors"] else None
    out.update(load=ld, hash=lt["hash"], lint=lt, summary=p.get("summary", ""), base_hash=p.get("base_hash"), client=p.get("client", ""),
               ts=(ev or {}).get("ts"), journaled=bool(ev) and p.get("hash") == lt["hash"],
               stale=bool(p.get("base_hash")) and p.get("base_hash") != out["master_hash"],
               diff="".join(difflib.unified_diff(mtext.splitlines(True), dtext.splitlines(True), m.name, d.name, n=2)))
    load_bad = bool(ld and ld["errors"])
    out["can_approve"] = out["journaled"] and not out["stale"] and lt["errors"] == 0 and not load_bad
    if not out["journaled"]:
        out["why_not"] = "the draft file was changed outside the assistant; ask the assistant to write it again"
    elif out["stale"]:
        out["why_not"] = "the master plan changed after this draft was written; ask the assistant to redo the draft on the current plan"
    elif lt["errors"]:
        out["why_not"] = f"the draft has {lt['errors']} lint error(s)"
    elif load_bad:
        out["why_not"] = (f"the draft does not load with the protocol ({ld['errors']} error(s)): "
                          + "; ".join(i["msg"] for i in ld["issues"][:3]))
    if with_text:
        out["text"] = dtext
    return out


def write(sess, text: str, summary: str, ident: dict) -> dict:
    text = str(text or "")
    if not text.strip():
        raise CmdError("the draft is empty", 422)
    if len(text.encode("utf-8")) > MAX_PLAN_BYTES:
        raise CmdError(f"the draft is larger than {MAX_PLAN_BYTES // 1024} KiB", 413)
    summary = " ".join(str(summary or "").split())[:200]
    if not summary:
        raise CmdError("a draft needs a one-line summary of the change (it becomes the revision row)", 422)
    m, d = master_path(sess), draft_path(sess)
    with _lock(sess):
        return _write_locked(sess, m, d, text, summary, ident)


def _write_locked(sess, m, d, text, summary, ident) -> dict:
    mtext = _text(m)
    if not text.endswith("\n"):
        text += "\n"
    _atomic_write(d, text)
    lt = lint(sess, d)
    sess.engine._emit("plan_draft", {"source": "assistant", "client": ident.get("client", ""), "session": ident.get("session", ""),
                                     "file": d.name, "hash": lt["hash"], "base_hash": planfmt.file_hash(mtext) if mtext else None,
                                     "summary": summary, "errors": lt["errors"], "warnings": lt["warnings"], "tests": lt["tests"]})
    st = status(sess, with_text=False)
    ld = st.get("load") or {}
    if lt["errors"]:
        nxt = "Fix every lint error and write the draft again; Approve stays disabled until the draft lints clean."
    elif ld.get("errors"):
        nxt = ("The draft lints clean but does not load with the protocol (" + "; ".join(i["msg"] for i in ld["issues"][:3])
               + "). Fix it and write the draft again; Approve stays disabled.")
    else:
        nxt = "The operator reviews the diff in design step 4 (Test plan) and presses Approve or Reject."
    return {"ok": True, "file": str(d), "hash": lt["hash"], "lint": lt, "load": ld or None, "diff": st.get("diff", ""), "next": nxt}


_REV_HEAD = re.compile(r"^##\s+Revision history\s*$", re.I | re.M)


def revision_rows(text: str) -> list[str]:
    """The data rows of the `## Revision history` table (header and separator left out), whitespace-normalised."""
    m = _REV_HEAD.search(text)
    if not m:
        return []
    start = m.end()
    nxt = re.search(r"^#{1,2}\s", text[start:], re.M)
    block = text[start:start + nxt.start()] if nxt else text[start:]
    rows = [" ".join(ln.split()) for ln in block.splitlines() if ln.lstrip().startswith("|")]
    return [r for r in rows if not re.match(r"^\|[\s:|-]+\|?$", r) and not re.match(r"^\|\s*revision\s*\|", r, re.I)]


def _row_label(row: str) -> str:
    m = re.match(r"^\|\s*([^|]*?)\s*\|", row)
    return m.group(1) if m else ""


def revision_issues(master_text: str, draft_text: str, name: str) -> list[dict]:
    """Lint errors for the revision-history rule: the draft keeps every existing row, any row it adds is labelled rN, and it adds at most
    one (approval adds the row itself when the draft adds none, so after approval exactly one new row exists)."""
    m = _REV_HEAD.search(draft_text)
    line = draft_text[:m.start()].count("\n") + 1 if m else 1

    def err(msg):
        return {"severity": "error", "file": name, "line": line, "msg": msg}
    old = [_row_label(r) for r in revision_rows(master_text)]
    new = [_row_label(r) for r in revision_rows(draft_text)]
    out = []
    rest = list(new)
    gone = []
    for lab in old:
        if lab in rest:
            rest.remove(lab)
        else:
            gone.append(lab)
    if gone:
        out.append(err("the draft deletes or renames existing revision row(s) " + ", ".join(gone) + ": the revision history is append-only"))
    bad = [lab for lab in rest if not re.fullmatch(r"r\d+", lab)]
    if bad:
        out.append(err("a new revision row must be labelled rN (r1, r2, ...), not " + ", ".join(repr(b) for b in bad)))
    good = [lab for lab in rest if re.fullmatch(r"r\d+", lab)]
    if len(good) > 1:
        out.append(err(f"the draft adds {len(good)} revision rows ({', '.join(good)}): add one row, or none and approval adds it"))
    return out


def draft_added_revision(master_text: str, draft_text: str) -> str | None:
    """The label (rN) of a revision row the draft added itself (a label the master does not have), or None. Editing the wording of an
    existing row is not adding one."""
    def labels(t):
        return [m.group(1) for r in revision_rows(t) if (m := re.match(r"^\|\s*(r\d+)\s*\|", r))]
    old = set(labels(master_text))
    new = [x for x in labels(draft_text) if x not in old]
    return new[-1] if new else None


def add_revision_row(text: str, summary: str, operator: str, date: str) -> tuple[str, str]:
    """Append a row to the `## Revision history` table (created when absent). Returns (new text, revision label)."""
    clean = summary.replace("|", "\\|")
    m = _REV_HEAD.search(text)
    if not m:
        rev = "r1"
        row = f"| {rev} | {date} | {clean} | AI draft approved by {operator} |\n"
        tail = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        return text + tail + "## Revision history\n\n| Revision | Date | Change | Source |\n|---|---|---|---|\n" + row, rev
    start = m.end()
    nxt = re.search(r"^#{1,2}\s", text[start:], re.M)
    end = start + nxt.start() if nxt else len(text)
    block = text[start:end]
    lines = block.splitlines(True)
    table_idx = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("|")]
    nums = [int(x) for x in re.findall(r"^\|\s*r(\d+)\s*\|", block, re.M)]
    rev = f"r{(max(nums) + 1) if nums else 1}"
    row = f"| {rev} | {date} | {clean} | AI draft approved by {operator} |\n"
    if table_idx:
        last = table_idx[-1]
        if not lines[last].endswith("\n"):
            lines[last] += "\n"
        lines.insert(last + 1, row)
    else:
        lines.append("\n| Revision | Date | Change | Source |\n|---|---|---|---|\n" + row)
    return text[:start] + "".join(lines) + text[end:], rev


def _atomic_write(path: Path, text: str):
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def approve(sess, draft_hash: str, operator: str) -> dict:
    operator = " ".join(str(operator or "").split())
    if not operator:
        raise CmdError("operator name required: plan approval records who approved", 422)
    with _lock(sess):                    # one approval at a time; the state is read again inside the lock
        st = status(sess, with_text=True, fresh=True)
        if not st.get("draft_exists"):
            raise CmdError("there is no draft to approve (it may already have been approved or rejected)", 404)
        if draft_hash != st["hash"]:
            raise CmdError("the draft changed since you opened it; review it again", 409)
        if not st["can_approve"]:
            raise CmdError("cannot approve: " + st.get("why_not", "unknown"), 409, lint=st["lint"], load=st.get("load"))
        m, d = Path(st["master"]), Path(st["draft"])
        old = _text(m)
        if planfmt.file_hash(old) != (st.get("base_hash") or planfmt.file_hash(old)):
            raise CmdError("the master plan changed after this draft was written", 409)
        own = draft_added_revision(old, st["text"])
        if own:                          # the draft carries its own revision row: approval does not add a second one
            new, rev = st["text"], own
        else:
            new, rev = add_revision_row(st["text"], st["summary"] or "AI draft", operator, time.strftime("%Y-%m-%d"))
        if len(revision_rows(new)) != len(revision_rows(old)) + 1:
            raise CmdError("cannot approve: the revision history must gain exactly one row", 409)
        hist = sess.dir / "ai" / "plan_history"
        hist.mkdir(parents=True, exist_ok=True)
        backup = hist / f"{m.stem}_{st['master_hash'] or 'none'}.md"
        if old:
            backup.write_text(old, encoding="utf-8")
        _atomic_write(m, new)
        # the plan that is live now is also the newest "last good plan" for the startup fallback (ai/recovery.py)
        (hist / f"{m.stem}_{planfmt.file_hash(new)}.md").write_text(new, encoding="utf-8")
        d.unlink(missing_ok=True)
        new_hash = planfmt.file_hash(new)
        ld = st.get("load") or {}
        sess.engine._emit("plan_approved", {"file": m.name, "old_hash": st["master_hash"], "new_hash": new_hash, "draft_hash": st["hash"],
                                            "revision": rev, "revision_row": "draft" if own else "added", "summary": st["summary"],
                                            "operator": operator, "client": st.get("client", ""), "backup": str(backup) if old else None,
                                            "tests_added": ld.get("added", []), "tests_retired": ld.get("retired", [])})
        return {"ok": True, "file": str(m), "old_hash": st["master_hash"], "new_hash": new_hash, "revision": rev,
                "revision_row": "draft" if own else "added", "tests_added": ld.get("added", []), "tests_retired": ld.get("retired", [])}


def reject(sess, reason: str, operator: str) -> dict:
    reason = " ".join(str(reason or "").split())
    if not reason:
        raise CmdError("a reject needs a reason (the assistant reads it)", 422)
    with _lock(sess):
        return _reject_locked(sess, reason, operator)


def _reject_locked(sess, reason, operator) -> dict:
    d = draft_path(sess)
    if not d.is_file():
        raise CmdError("there is no draft to reject", 404)
    h = planfmt.file_hash(_text(d))
    dst_dir = sess.dir / "ai" / "rejected"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{d.stem}_{h}_{time.strftime('%Y%m%d-%H%M%S')}.md"
    shutil.move(str(d), dst)
    sess.engine._emit("plan_draft_rejected", {"file": d.name, "hash": h, "reason": reason, "moved_to": str(dst),
                                              "operator": " ".join(str(operator or "").split())})
    return {"ok": True, "moved_to": str(dst)}
