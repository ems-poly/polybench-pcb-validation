"""Action `dmm.read`: wait for one stable reading of the UNI-T multimeter (UT161E over the CH9329 cable) and propose it.

params:
  timeout_s (10)   how long to wait for a stable reading (the action owns this timeout)
  unit             the plan unit to propose in (V, mV, Ohm, kOhm, MOhm, A, mA, uA, Hz, kHz, MHz, %, degC). Same quantity as the
                   meter's reading, else the action fails and says what the meter is measuring. Omit to propose in the meter's unit.
  n (5), pct (0.2) the stability rule (same defaults as the live card, TBR)
  map {value: <test_id>}   which test receives the value (never hard-coded here)
  sim {scene, mode, range, value}   (simulator only) what the fake meter shows; scenes: dc_v, dc_mv, ohm, ohm_ol, hold, ac_v, freq, ma

Device ownership (R15): ONE owner of the HID handle. When the server runs the action the live reader in the server is paused (it closes
the cable) before the child starts and resumed when the child ends (runner.py device hooks), so the two never fight. The child then opens
the cable itself, sends only Get Name and Get Measurement, and closes it in `finally`. Run from a terminal while a server holds the
cable, it fails with a plain message.

Uses the same frame check, decode, per-model table and stability rule as the live card. No verdicts: the core evaluates the value.
Readiness: HOST-ONLY (simulated meter; the stable-reading path is exercised against fakes). The live card's reader was exercised on a
real UT161E for the modes listed in docs/MULTIMETER.md.
"""
from __future__ import annotations

from ._util import ParamError, PluginError, timeout_param, to_float, to_int


class DmmRead:
    name = "dmm.read"
    capabilities = ["multimeter"]
    holds_device = True            # runner.py: the live reader is paused while this action runs

    def run(self, ctx) -> None:
        from ..meter import reading as R
        from ..meter import transport as T
        from ..meter.link import MeterLink, read_stable
        p = ctx.params
        timeout = timeout_param(p.get("timeout_s", 10))
        n = to_int(p.get("n", 5), "n")
        pct = to_float(p.get("pct", 0.2), "pct")
        if not 2 <= n <= 50 or not 0 <= pct <= 10:
            raise ParamError("n must be 2..50 and pct 0..10")
        unit = p.get("unit") or None
        if unit:
            from ..meter import units
            if units.quantity(unit) is None:
                raise ParamError(f"unit '{unit}' is not one the meter can give")
        test_id = (p.get("map") or {}).get("value")
        if ctx.simulate:
            sim = p.get("sim") or {}
            tr = T.SimTransport(latency_s=0.002, noise=True)
            tr.set_scene(sim.get("scene") or "dc_v", **{k: v for k, v in {"value": sim.get("value"), "mode": sim.get("mode"),
                                                                           "range_idx": sim.get("range")}.items() if v is not None})
        else:
            tr = T.HidTransport()
        try:
            tr.open()
        except T.TransportError as e:
            raise PluginError(f"{e}. Plug in the CH9329 cable; if the Polybench live meter card holds it, it is paused automatically "
                              "for a run from the Polybench page (not for a run from a terminal).") from None
        try:
            link = MeterLink(tr)
            model = link.get_name() or link.get_name()
            if not model:
                raise PluginError("the cable is there but the meter does not answer: is it on, awake, and is its USB function enabled?")
            ctx.log(f"meter: {model}")
            r, why = read_stable(link, model, timeout, n, pct, ctx.cancelled)
        finally:
            tr.close()
        if ctx.cancelled():
            ctx.log("cancelled", "warn")
            return
        if r is None:
            raise PluginError(f"no stable reading within {timeout:g} s: {why}")
        value, unit_out = R.label(r), r.unit
        if unit:
            conv, bad = R.to_unit(r, unit)
            if conv is None:
                raise PluginError(bad)
            value, unit_out = conv, unit
        else:
            value = r.text
        m = r.meas
        evidence = (f"{model} mode {r.mode_name} (0x{m.mode:02x}) range {m.range_idx} display '{m.display}' "
                    f"flags {m.flag_bytes[0]:02x} {m.flag_bytes[1]:02x} {m.flag_bytes[2]:02x} frame {m.hex}")
        ctx.log(f"stable reading: {R.label(r)} ({r.mode_name}, range {m.range_idx}, table entry {r.status})")
        if test_id:
            ctx.propose(test_id, value, unit_out, evidence, m.hex)

    def safe_state(self) -> str:
        return "dmm.read holds nothing: the cable is opened only inside run and closed in finally; it sends no output-changing command"

    def selftest(self) -> bool:
        from .. import meter
        return meter.selftest()
