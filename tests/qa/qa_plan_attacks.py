"""QA: plan-approval attacks against the staging server on 8903 (HTTP only, operator = no header, assistant = header)."""
import json, re, threading, time, urllib.request, urllib.error, sys
from pathlib import Path

import os
URL = os.environ.get("QA_URL", "http://127.0.0.1:8920")
AI = {"X-Polybench-Client": "assistant", "X-Polybench-Assistant": "qa", "X-Polybench-Session": "s1"}
PROJ = Path(os.environ["QA_PROJ"])
MASTER = PROJ / "PLAN.md"


def req(method, path, body=None, ai=False):
    h = {"Content-Type": "application/json"} | (AI if ai else {"Cookie": "pb_op=" + os.environ["POLYBENCH_OP_TOKEN"]})
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(URL + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        b = e.read()
        try:
            return e.code, json.loads(b)
        except ValueError:
            return e.code, {"raw": b[:200].decode(errors="replace")}


def draft(text, summary):
    return req("POST", "/api/ai/plan_draft", {"text": text, "summary": summary}, ai=True)


def status():
    return req("GET", "/api/ai/plan_draft")[1]


def approve(h, op="Pat"):
    return req("POST", "/api/ai/plan_approve", {"hash": h, "operator": op})


def journal(types):
    return [e for e in req("GET", "/api/journal")[1] if e["type"] in types] if isinstance(req("GET", "/api/journal")[1], list) else req("GET", "/api/ai/journal?limit=200&types=" + ",".join(types), ai=True)[1]["events"]


print("### A1. draft that already carries its own revision row -> approve")
m0 = MASTER.read_text(encoding="utf-8")
text = m0.replace("| U06 | R4 across its pads |", "| U06 | R4 across its two pads |")
text = text.rstrip("\n") + "\n| r2 | 2026-10-17 | U06 wording (row added by the assistant) | assistant |\n"
code, r = draft(text, "U06 wording")
print(" draft:", code, r.get("lint", {}).get("errors"), r.get("hash"))
st = status()
code, r = approve(st["hash"])
print(" approve:", code, r)
out = MASTER.read_text(encoding="utf-8")
rows = re.findall(r"^\| r\d+ \|.*$", out, re.M)
print(" revision rows now:", *rows, sep="\n   ")

print("### A2. operator name forged / missing")
text2 = out.replace("two pads", "2 pads")
code, r = draft(text2, "U06 wording again")
st = status()
print(" approve with empty operator:", approve(st["hash"], "")[0])
print(" approve by assistant header with operator Pat:", req("POST", "/api/ai/plan_approve", {"hash": st["hash"], "operator": "Pat"}, ai=True)[0])

print("### A3. concurrent approve x3 from 'two tabs'")
res = []
def go():
    res.append(approve(st["hash"], "TabA"))
ts = [threading.Thread(target=go) for _ in range(3)]
[t.start() for t in ts]; [t.join() for t in ts]
print(" results:", [(c, (b.get("error") or b.get("revision"))) for c, b in res])
ev = req("GET", "/api/ai/journal?limit=200&types=plan_approved", ai=True)[1]["events"]
print(" plan_approved events:", len(ev), [e["payload"]["revision"] for e in ev])
print(" revision rows now:", re.findall(r"^\| r\d+ \|.*$", MASTER.read_text(encoding="utf-8"), re.M))

print("### A4. record a value on U06 then approve a plan that drops U06")
print(" entry:", req("POST", "/api/cmd/entry", {"board": "S01", "test_id": "U06", "value": "10.0", "confirm": True})[0])
cur = MASTER.read_text(encoding="utf-8")
dropped = "\n".join(l for l in cur.splitlines() if not l.startswith("| U06 |")) + "\n"
code, r = draft(dropped, "drop U06")
print(" draft:", code, r.get("lint", {}).get("errors"), [i["msg"] for i in r.get("lint", {}).get("issues", [])][:3])
st = status()
code, r = approve(st["hash"])
print(" approve:", code, {k: r.get(k) for k in ("ok", "revision", "reload", "error")})
s = req("GET", "/api/state")[1]
print(" U06 in test_index after reload:", "S01|U06" in s.get("cells", {}), "retired:", [x.get("id") for x in s.get("retired", [])])
print(" state via ai board S01:", [t.get("test_id") for st_ in req("GET", "/api/ai/board?board=S01", ai=True)[1]["steps"] for t in st_["tests"]])
tp = req("GET", "/api/ai/plan", ai=True)[1]
print(" plan report retired:", tp["report"].get("retired"))

print("### A5. plan that drops P01..P04 which action read_rails `proposes` -> approve")
cur = MASTER.read_text(encoding="utf-8")
dropped = "\n".join(l for l in cur.splitlines() if not re.match(r"\| P0[1-4] \|", l)) + "\n"
code, r = draft(dropped, "drop P01-P04")
print(" draft lint:", code, r.get("lint", {}).get("errors"), [i["msg"] for i in r.get("lint", {}).get("issues", [])][:4])
st = status()
code, r = approve(st["hash"])
print(" approve:", code, {k: r.get(k) for k in ("ok", "revision", "reload", "error")})
print(" master now has P01?", "| P01 |" in MASTER.read_text(encoding="utf-8"), " backup files:", sorted(p.name for p in Path(os.environ["QA_DATA"]).rglob("plan_history/*"))[:6])

print("### A6. stale: draft, then master edited by hand")
cur = MASTER.read_text(encoding="utf-8")
code, r = draft(cur.replace("First issue", "First issue!"), "stale test")
MASTER.write_text(cur + "\n", encoding="utf-8")
st = status(); print(" stale:", st["stale"], "can_approve:", st["can_approve"], "approve ->", approve(st["hash"])[0])
MASTER.write_text(cur, encoding="utf-8")
st = status(); print(" after restoring master: stale:", st["stale"], "can_approve:", st["can_approve"])
print(" reject:", req("POST", "/api/ai/plan_reject", {"reason": "qa cleanup", "operator": "Pat"})[0])

print("### A7. draft with lint error and draft of 600 KiB")
code, r = draft(cur.replace("| hard | calc C2 |", "| hard | estimate |", 1), "lint error")
st = status(); print(" errors:", st["lint"]["errors"], "approve ->", approve(st["hash"]))
print(" big:", draft("x" * 600 * 1024, "big")[0])
print(" reject:", req("POST", "/api/ai/plan_reject", {"reason": "qa cleanup 2", "operator": "Pat"})[0])
print("journal head", req("GET", "/api/ai/journal?limit=1", ai=True)[1]["head"])
