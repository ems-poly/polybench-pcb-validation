"""Built-in demo actions (names `demo.*`) so the example protocol runs with no plugin installed.

They only propose values; the core computes verdicts. Values are deterministic per (board, test).
"""
from __future__ import annotations

import hashlib
import math
import random
import time
from pathlib import Path


def _rng(*parts) -> random.Random:
    return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16))


class DemoMeasure:
    """params: values = {TEST_ID: [nominal, rel_spread, unit]}, force = {"S03:U01": value},
    analyse = {trip_above: {TEST_ID: limit}} (optional): also report an analysis block {tripped, within_limit, ...} and the readings as
    samples, for the Results explorer and `derive` rules."""
    name = "demo.measure"
    capabilities: list[str] = []

    def run(self, ctx):
        values = ctx.params.get("values", {})
        force = ctx.params.get("force", {})
        n = max(len(values), 1)
        ctx.log(f"demo.measure on {ctx.board}: {len(values)} readings")
        got, rows = {}, []
        for i, (tid, spec) in enumerate(values.items()):
            if ctx.cancelled():
                ctx.log("cancelled", "warn")
                return
            nominal, spread, unit = spec[0], spec[1], (spec[2] if len(spec) > 2 else None)
            v = force.get(f"{ctx.board}:{tid}")
            if v is None:
                v = nominal * (1 + _rng(ctx.board, tid).uniform(-spread, spread))
            time.sleep(0.12)
            ctx.progress((i + 1) / n, f"{tid}")
            ctx.propose(tid, f"{v:.4g}", unit, raw=f"sim {tid}={v:.6g}")
            got[tid] = v
            rows.append({"t_s": round(0.12 * (i + 1), 3), "channel": 1, "quantity": f"reading for {tid}", "value": float(f"{v:.4g}"), "unit": unit, "mode": "CV", "test": tid})
        an = ctx.params.get("analyse")
        if an is not None:
            over = {t: got[t] for t, lim in (an.get("trip_above") or {}).items() if t in got and got[t] > lim}
            ctx.analysis({"tripped": bool(over), "within_limit": not over, "readings": len(got), "over": over, "settled": True}, rows)
        ctx.log("done")

    def safe_state(self):
        return "demo: no outputs to turn off"

    def selftest(self):
        return True


class DemoSweep:
    """A load sweep that proposes one table. params: test, loads (mA), v0 (V at no load), droop (V per mA), force_droop = {"S03": V per mA}"""
    name = "demo.sweep"
    capabilities: list[str] = []

    def run(self, ctx):
        loads = ctx.params.get("loads", [0, 10, 20, 40, 80])
        v0 = float(ctx.params.get("v0", 5.0))
        droop = float(ctx.params.get("force_droop", {}).get(ctx.board, ctx.params.get("droop", 0.0006)))
        rows = []
        for i, ma in enumerate(loads):
            if ctx.cancelled():
                ctx.log("cancelled", "warn")
                return
            time.sleep(0.1)
            ctx.progress((i + 1) / len(loads), f"{ma} mA")
            rows.append([ma, round(v0 - droop * ma, 4)])
        ctx.propose_table(ctx.params["test"], rows, ["load_ma", "rail_v"], raw=f"sim sweep, {len(rows)} points")
        ctx.log(f"swept {len(rows)} load points")

    def safe_state(self):
        return "demo: no outputs to turn off"

    def selftest(self):
        return True


class DemoFlash:
    """params: device_id_test, ok_test"""
    name = "demo.flash"
    capabilities = ["programmer"]

    def run(self, ctx):
        steps = ["Connecting to simulated Atmel-ICE", "Erasing", "Programming 61440 bytes", "Verifying", "Reset"]
        for i, s in enumerate(steps):
            if ctx.cancelled():
                ctx.log("cancelled", "warn")
                return
            ctx.log(s)
            ctx.progress((i + 1) / len(steps), s)
            time.sleep(0.25)
        if ctx.params.get("device_id_test"):
            ctx.propose(ctx.params["device_id_test"], "0x10810A05", None, raw="DEVICE_ID simulated")
        if ctx.params.get("ok_test"):
            ctx.propose(ctx.params["ok_test"], "yes", None, raw="verify OK")

    def safe_state(self):
        return "demo programmer: target released"

    def selftest(self):
        return True


class DemoCapture:
    """Guided capture: asks the operator, shows progress, proposes statistics.
    params: ask, captures (default 4), tests = {"mean": "C01", "std": "C02", "pp": "C03"}"""
    name = "demo.capture"
    capabilities = ["bus_adapter"]

    def run(self, ctx):
        ans = ctx.ask(ctx.params.get("ask", "Connect the bridge simulator at 2 mV/V, then continue"),
                      ["Connected", "Abort"])
        if ans != "Connected":
            raise RuntimeError("operator aborted the capture")
        n = int(ctx.params.get("captures", 4))
        rng = _rng(ctx.board, "capture")
        data = []
        for i in range(n):
            if ctx.cancelled():
                ctx.log("cancelled", "warn")
                return
            ctx.log(f"capture {i + 1}/{n}: 250 samples")
            time.sleep(0.3)
            data += [2.0 + rng.gauss(0, 0.0004) for _ in range(250)]
            ctx.progress((i + 1) / n, f"capture {i + 1}/{n}")
        mean = sum(data) / len(data)
        std = math.sqrt(sum((x - mean) ** 2 for x in data) / (len(data) - 1))
        pp = max(data) - min(data)
        t = ctx.params.get("tests", {})
        if "mean" in t:
            ctx.propose(t["mean"], f"{mean:.5f}", "mV/V", raw=f"n={len(data)}")
        if "std" in t:
            ctx.propose(t["std"], f"{std * 1000:.3f}", "uV/V", raw=f"n={len(data)}")
        if "pp" in t:
            ctx.propose(t["pp"], f"{pp * 1000:.2f}", "uV/V", raw=f"n={len(data)}")
        ctx.log(f"mean {mean:.5f} mV/V, std {std * 1000:.3f} uV/V, pk-pk {pp * 1000:.2f} uV/V")

    def safe_state(self):
        return "demo bus adapter: released"

    def selftest(self):
        return True


class DemoSlow:
    """Runs until cancelled. For testing abort."""
    name = "demo.slow"
    capabilities: list[str] = []

    def run(self, ctx):
        for i in range(600):
            if ctx.cancelled():
                ctx.log("cancel seen, stopping", "warn")
                return
            ctx.progress(min(i / 600, 0.99), f"step {i}")
            time.sleep(0.1)

    def safe_state(self):
        return "demo.slow: outputs off (read back: off)"

    def selftest(self):
        return True


class DemoFail:
    name = "demo.fail"
    capabilities: list[str] = []

    def run(self, ctx):
        """params: until_file (optional): fails until that file exists, then proposes `propose: {test: value}` (rehearses 'plug the probe in,
        press Retry'); propose: {test: value}."""
        uf = ctx.params.get("until_file")
        if uf and Path(str(uf)).exists():
            ctx.log("simulated: the programmer answered this time")
            for tid, v in (ctx.params.get("propose") or {}).items():
                ctx.propose(tid, v, None, raw="simulated")
            return
        ctx.log("about to fail", "warn")
        raise RuntimeError("simulated hardware failure: programmer not found")

    def safe_state(self):
        return "demo.fail: nothing to do"

    def selftest(self):
        return True


ACTIONS = {c.name: c for c in (DemoMeasure, DemoSweep, DemoFlash, DemoCapture, DemoSlow, DemoFail)}
