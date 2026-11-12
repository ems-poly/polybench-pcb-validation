"""`polybench selftest`: quick no-hardware check that the install works end to end."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from importlib import resources
from pathlib import Path

# The demo ships as package data (polybench/examples/demo), so this works from a wheel as well as from a checkout.
DEMO = Path(str(resources.files("polybench") / "examples" / "demo" / "demo.pcbv.yaml"))


def run() -> int:
    from . import registry
    from .engine import Engine, evaluate
    from .protocol import load
    from .report import build_model, to_html, to_markdown
    from .store import Journal

    results = []

    def check(name, fn):
        try:
            ok = fn()
            results.append((name, bool(ok), ""))
        except Exception as e:  # noqa: BLE001
            results.append((name, False, f"{type(e).__name__}: {e}"))

    proto = load(DEMO)
    check("demo protocol loads, no lint errors", lambda: not proto.errors and len(list(proto.tests())) > 15)
    tmp = Path(tempfile.mkdtemp(prefix="pbself-"))

    def store():
        j = Journal(tmp / "j.jsonl")
        eng = Engine(proto, j, "selftest")
        eng.create_batch("self", ["S01", "S02"], "S", True, "DEMO-1")
        eng.entry("S01", "V01", "y")
        n = len(j.events)
        j.close()
        j2 = Journal(tmp / "j.jsonl")
        e2 = Engine(proto, j2, "selftest")
        return len(j2.events) == n and e2.state.cell("S01", "V01")["value"] == "yes" and not j2.problems

    check("journal write, fsync, replay", store)
    t = proto.test("U01")[2]
    check("limit classes: hard fails, guess reviews, tbr silent",
          lambda: evaluate(t, "5")["verdict"] == "fail" and evaluate(proto.test("U02")[2], "1")["verdict"] == "review"
          and evaluate(proto.test("U03")[2], "0.5")["verdict"] == "none")

    def report():
        j = Journal(tmp / "j.jsonl")
        eng = Engine(proto, j, "selftest")
        m = build_model(eng.state)
        return "<table" in to_html(m) and to_markdown(m).startswith("# ")

    check("report renders (markdown and HTML)", report)
    check("action registry lists demo actions", lambda: {"demo.measure", "demo.flash"} <= registry.action_names())

    def child():
        # stdin must stay open while the child runs: EOF on stdin means "the parent died" and cancels the action
        proc = subprocess.Popen([sys.executable, "-m", "polybench.run_action", "demo.measure",
                                 json.dumps({"board": "S01", "params": {"values": {"P01": [20, 0.1, "mA"]}},
                                             "workdir": str(tmp / "w"), "simulate": True})],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            out = proc.stdout.read()
            proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.stdin.close()
        kinds = [json.loads(l)["t"] for l in out.splitlines() if l.startswith("{")]
        return "proposal" in kinds and kinds[-1] == "done"

    check("action child process speaks JSON lines", child)
    bridge, note = registry.load_ecad(None, True)
    check("simulated ECAD bridge highlights", lambda: bridge.highlight(["U1"])["highlighted"] == ["U1"])

    def kicad_selftest():
        from .kicad.bridge import KicadBridge
        return KicadBridge(simulate=True).selftest()
    check("KiCad bridge selftest (fake KiCad API server, no KiCad needed)", kicad_selftest)
    def meter_selftest():
        from . import meter
        return meter.selftest()
    check("multimeter frames, parser, units and stability rule (no hardware)", meter_selftest)
    width = max(len(n) for n, _, _ in results)
    for n, ok, msg in results:
        print(f"{'PASS' if ok else 'FAIL'}  {n.ljust(width)}  {msg}")
    bad = [r for r in results if not r[1]]
    print(f"\nselftest: {len(results) - len(bad)}/{len(results)} passed (HOST-ONLY: no hardware touched)")
    return 1 if bad else 0
