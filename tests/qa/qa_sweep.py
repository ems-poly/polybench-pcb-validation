"""QA: sweep every route of the app with the assistant identity in each permission mode; record status and whether a
refusal was journaled. TestClient on a scratch copy; nothing touches 8750 or ~/polybench-data."""
import os, re, shutil, sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from helpers import DEMO, ROOT

AI = {"X-Polybench-Client": "assistant", "X-Polybench-Assistant": "qa", "X-Polybench-Session": "s1"}
PLAN = (ROOT / "tests" / "data" / "DEMO_TEST_PLAN.md").read_text()
OUT = Path(sys.argv[1])


def make(mode):
    from polybench.protocol import load
    from polybench.server import Session, create_app
    if mode is None:
        os.environ.pop("POLYBENCH_AI_PERMISSIONS", None)
    else:
        os.environ["POLYBENCH_AI_PERMISSIONS"] = mode
    d = OUT / f"proj_{mode}"
    shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(DEMO, d)
    top = d / "demo.pcbv.yaml"
    (d / "PLAN.md").write_text(PLAN)
    top.write_text(top.read_text() + "\ntest_plan: {file: PLAN.md, generate: true}\n")
    sess = Session(load(top), OUT / f"data_{mode}", "ai", True, 3, None, operator="Op")
    app = create_app(sess)
    return sess, TestClient(app), app


FILL = {"{name}": "accept", "{path:path}": "x.jpg", "{board}": "S01"}
BODIES = {"/api/cmd/{name}": {"board": "S01", "test_id": "U01", "proposal_id": 1}}

rows = []
for mode in (None, "read", "propose", "off", "ADMIN", "Propose "):
    sess, c, app = make(mode)
    for r in app.routes:
        path = getattr(r, "path", None)
        methods = getattr(r, "methods", None)
        if not path or not methods or path.startswith("/static"):
            continue
        p = path
        for k, v in FILL.items():
            p = p.replace(k, v)
        for m in sorted(methods):
            if m == "HEAD":
                continue
            n = len(sess.journal.events)
            try:
                if m == "GET":
                    resp = c.get(p, headers=AI)
                else:
                    resp = c.request(m, p, json=BODIES.get(path, {}), headers=AI)
                code = resp.status_code
            except Exception as e:
                code = f"EXC {type(e).__name__}"
            journaled = any(e["type"] == "ai_action" and e["payload"].get("outcome") == "refused" for e in sess.journal.events[n:])
            rows.append({"mode": str(mode), "method": m, "path": path, "status": code, "refusal_journaled": journaled})
json.dump(rows, open(OUT / "sweep.json", "w"), indent=0)
# summary: anything that is not 403 with the assistant identity, per mode
for mode in (None, "read", "propose", "off", "ADMIN", "Propose "):
    ok = [f"{r['method']} {r['path']} -> {r['status']}" for r in rows if r["mode"] == str(mode) and r["status"] != 403]
    print(f"== mode={mode!r}: {len(ok)} routes NOT refused")
    for x in ok:
        print("   ", x)
# refusals on non-GET not journaled
miss = [f"{r['mode']} {r['method']} {r['path']}" for r in rows if r["status"] == 403 and not r["refusal_journaled"] and r["method"] != "GET"]
print("== non-GET refusals NOT journaled:", len(miss))
for x in miss:
    print("   ", x)
print("== GET refusals not journaled:", sum(1 for r in rows if r["status"] == 403 and not r["refusal_journaled"] and r["method"] == "GET"))
