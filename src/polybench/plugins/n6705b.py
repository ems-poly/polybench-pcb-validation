"""Actions `n6705b.identify`, `n6705b.first_power`, `n6705b.measure`, `n6705b.load_sweep` on the Keysight N6705B, through the separate polystage-bench library.

The library (`polystage_bench`) owns every safety rule and these actions obey its contract:

  * limits come from a DUT profile TOML (`profile` param: a name in ~/.config/polystage-bench/profiles or a path), never
    from numbers in the params. A param may only ask for a LOWER current limit than the profile's; a higher one is refused.
  * the outputs are switched off on every exit path (success, error, cancel), read back with OUTP?, and the result logged.
  * one controller per instrument: each action opens one session and closes it; the instrument is found by serial
    (~/.config/polystage-bench/instruments.toml), or by `host` for a given address.
  * no external kill timers. The frame's output watchdog is armed by the library's set_output only while an output is on and
    is disarmed when all outputs are off. `leave: on` therefore lasts only until the profile's watchdog delay expires:
    nobody keeps the session alive after the action ends. Default is `leave: off`.

Common params: instrument (name in instruments.toml), host, serial, subnet, port, timeout_s (5), patience_s (12; the library
default of 40 s is too long for a bench UI), channel (1), transport ("lan").
"""
from __future__ import annotations

import csv
import json
import math
import os
from pathlib import Path

from ._util import ParamError, PluginError, fmt_num, need, safe_name, stamp, to_float, to_int

STATE_FILE = Path(os.environ.get("POLYBENCH_STATE_DIR", Path.home() / ".cache" / "polybench")) / "n6705b_last.json"
CURRENT_SCALE = {"A": 1.0, "mA": 1e3, "uA": 1e6}


def _lib():
    try:
        from polystage_bench import n6705b_scpi, profiles, config
    except ImportError:
        raise PluginError("the separate polystage-bench library is not installed in this Python, so the n6705b actions are unavailable") from None
    return n6705b_scpi, profiles, config


def load_profile(p: dict, simulate: bool, required: bool):
    _, profiles, _ = _lib()
    name = p.get("profile")
    if not name:
        if required and not simulate:
            raise ParamError("param 'profile' is required: the limits of this job are a DUT profile TOML, not arguments")
        return profiles.Profile.safe_default()
    try:
        return profiles.Profile.load(str(name))
    except Exception as e:
        if simulate:
            return profiles.Profile.safe_default()   # a missing profile file must not stop a simulated click-through
        raise PluginError(f"profile {name!r}: {e}. Install the profile in ~/.config/polystage-bench/profiles/") from None


def open_frame(p: dict, simulate: bool, profile):
    lib, _, config = _lib()
    try:
        R = config.resolve("n6705b", p.get("instrument"), p.get("serial"), p.get("subnet"), p.get("host"),
                           to_int(p["port"]) if p.get("port") else None, simulator=simulate)
    except config.ConfigError as e:
        raise PluginError(str(e)) from None
    try:
        fr = lib.open_frame(R.host, R.port, to_float(p.get("timeout_s", 5.0), "timeout_s"),
                            "simulator" if simulate else "lan", R.serial or "", R.subnet, profile,
                            transport=p.get("transport", "lan"), patience_s=to_float(p.get("patience_s", 12.0), "patience_s"))
    except Exception as e:
        raise PluginError(f"Power analyzer not reachable: {e}. It is on a Wi-Fi extender: check it is powered and on "
                          "the network (polystage-discover bench-psu).") from None
    _watchdog_hygiene(fr)
    orig_close = fr.close

    def close(*a, **k):                     # every exit path: disarm a watchdog that has nothing left to guard
        if not (k.get("link_down") or (a and a[0])):   # never talk to a frame whose link is known to be down
            _watchdog_hygiene(fr)
        return orig_close(*a, **k)
    fr.close = close
    return fr


def _watchdog_hygiene(fr) -> None:
    """Disarm the frame's output watchdog when no output is sourcing or loading (19 Oct, bench): a watchdog left armed by an
    earlier action (first_power `leave: on`) latches PROT every time the frame sees no SCPI traffic for its delay, even with
    the outputs already off and while the operator works on the front panel. A channel in voltmeter emulation (VMET) sources
    nothing and does not count. Never raises; never changes an output."""
    try:
        if not getattr(fr, "channels", None):
            return
        outs = fr.outputs_now()
        live = []
        for ch, on in outs.items():
            if on:
                emul = (fr._try(f"EMUL? (@{ch})") or "").strip().strip('"').upper()
                if emul not in ("VMET", "VMETER"):
                    live.append(ch)
        if live:
            return                                     # an output is energised: the watchdog is its safety net
        wd = fr._try("OUTP:PROT:WDOG?")
        if wd is not None and int(float(wd)) != 0:
            fr.write("OUTP:PROT:WDOG OFF")
        if hasattr(fr, "_wdog_armed"):
            fr._wdog_armed = False
    except Exception:  # noqa
        pass


def _remember(p: dict, simulate: bool = False) -> None:
    """Remember how the last run reached the frame, so safe_state() (a fresh child with no params) can reach it too."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        keep = {k: p[k] for k in ("instrument", "host", "serial", "subnet", "port", "transport") if p.get(k)}
        keep["simulate"] = bool(simulate)
        STATE_FILE.write_text(json.dumps(keep))
    except OSError:
        pass


def _read_samples(fr, ch: int, n: int, ctx) -> list[dict]:
    out = []
    for i in range(n):
        if ctx.cancelled():
            raise PluginError("cancelled")
        out.append(fr.measure(ch))
        if i + 1 < n:
            fr._wait(0.1)
    return out


def _mean(xs):
    return sum(xs) / len(xs)


def _propose_vi(ctx, mp: dict, vs: list[float], is_: list[float], cu: str, digits: int = 5) -> None:
    sc = CURRENT_SCALE[cu]
    if mp.get("voltage"):
        ctx.propose(mp["voltage"], fmt_num(_mean(vs), digits), "V", None, f"mean of {len(vs)} readings")
    if mp.get("current"):
        ctx.propose(mp["current"], fmt_num(_mean(is_) * sc, digits), cu, None, f"mean of {len(is_)} readings")
    if mp.get("power"):
        ctx.propose(mp["power"], fmt_num(_mean(vs) * _mean(is_) * 1e3, digits), "mW", None, "V x I")


def _stats(xs: list[float]) -> dict:
    return {"min": min(xs), "max": max(xs), "mean": _mean(xs), "final": xs[-1], "n": len(xs)} if xs else {}


def _settled(xs: list[float], rel: float = 0.03, floor: float = 1e-4) -> bool:
    """The last readings agree within rel of their mean (floor in the unit of xs for a mean near zero): the value has stopped moving."""
    tail = xs[-5:]
    if len(tail) < 3:
        return False
    m = _mean(tail)
    return (max(tail) - min(tail)) <= max(abs(m) * rel, floor)


def _sample_rows(ch: int, rs: list[dict], cu: str, t0: float, step: float = 0.1) -> list[dict]:
    """Samples as explorer rows: one voltage, one current (in `cu`) and the CV/CC mode per reading."""
    sc = CURRENT_SCALE[cu]
    rows = []
    for k, r in enumerate(rs):
        t = round(t0 + k * step, 3)
        mode = r.get("regulation")
        rows.append({"t_s": t, "channel": ch, "quantity": "voltage", "value": r["v"], "unit": "V", "mode": mode, "map_key": "voltage"})
        rows.append({"t_s": t, "channel": ch, "quantity": "current", "value": r["i"] * sc, "unit": cu, "mode": mode, "map_key": "current"})
    return rows


def supply_analysis(rs: list[dict], cu: str, ilim_a: float | None, trips: list | None = None, ramp: dict | None = None,
                    tripped: bool | None = None, ramp_rs: list[dict] | None = None) -> dict:
    """The small analysis block of a supply read: min, max, mean, final of V and I, the mode (CV/CC) of every reading, whether the
    supply sat at its current limit, whether a protection tripped, whether the current had settled. Never computes a verdict:
    within_limit is a fact about the run (no CC, no trip, current below the limit), the protocol decides what it answers."""
    sc = CURRENT_SCALE[cu]
    vs, is_ = [r["v"] for r in rs], [r["i"] for r in rs]
    ramp_rs = ramp_rs or []
    modes = [r.get("regulation") for r in rs]
    allr = ramp_rs + rs                                  # the whole run, ramp steps included: "stayed under the limit all the way up"
    ramp = ramp or {}
    cc = any(r.get("regulation") == "CC" for r in allr) or ramp.get("regulation_after") == "CC" or ramp.get("regulation_before") == "CC"
    at_lim = [r["i"] for r in allr if ilim_a and r["i"] >= 0.98 * ilim_a]
    trips = list(trips or [])
    tr = bool(trips) if tripped is None else tripped
    out = {"voltage": _stats(vs), "current": {k: (v * sc if k != "n" else v) for k, v in _stats(is_).items()},
           "current_unit": cu, "modes": sorted({m for m in modes if m}), "final_mode": modes[-1] if modes else None,
           "cc_seen": cc, "limit_a": ilim_a, "limit": None if ilim_a is None else ilim_a * sc,
           "samples_at_limit": len(at_lim), "time_at_limit_s": round(len(at_lim) * 0.1, 3), "at_limit": bool(at_lim) or cc,
           "tripped": tr, "trips": trips, "settled": _settled([i * sc for i in is_], floor=0.01),
           "ramp_cv_cc_changed": bool(ramp.get("cv_cc_changed")),
           "ramp_current": {k: (v * sc if k != "n" else v) for k, v in _stats([r["i"] for r in ramp_rs]).items()}}
    out["within_limit"] = (not tr) and (not cc) and (not at_lim)
    return out


def ramp_steps_s(volts: float, start_v: float, step_v: float, dwell: float) -> float:
    return (math.ceil((volts - start_v) / step_v - 1e-9) * dwell) if volts > start_v + 1e-9 else 0.0


def _units(p: dict) -> str:
    cu = p.get("current_unit", "mA")
    if cu not in CURRENT_SCALE:
        raise ParamError(f"current_unit must be one of {list(CURRENT_SCALE)}")
    return cu


def profile_limits(fr, profile, ch: int) -> dict:
    """effective_limits of a channel, judged against the output range the PROFILE selects (output_range_v), not the
    range the frame happens to be on now (19 Oct: the frame sat on the 6 V range, 6.12 V max, and a 12 V request under a
    20 V-range profile was refused). The library selects that range at energise (VOLT:RANG, output OFF)."""
    lib = _lib()[0]
    info = fr.channels[ch]
    try:
        rng = profile.limits_for(ch).output_range_v
    except Exception:
        rng = None
    if rng is not None and info.get("model") == "N6781A":
        vcap, icap = type(fr).range_caps(rng)
        info = {**info, "vmax": vcap, "imax": icap}
    return lib.effective_limits(profile, ch, info)


def _cwait(fr, ctx, seconds: float, chunk: float = 0.2) -> None:
    """fr._wait in short chunks so a cancel (UI Stop, voice "stop") is seen within about `chunk` s, not after a whole dwell."""
    end = fr._now() + seconds
    while True:
        if ctx.cancelled():
            raise PluginError("cancelled")
        left = end - fr._now()
        if left <= 0:
            return
        fr._wait(min(chunk, left))


def _sim_rail_model(fr, p: dict) -> None:
    """Simulator only: a DUT rail behind the source and the load channel. The library's simulator has no sagging rail,
    so this replaces its electrical model on THIS simulated frame. Params: sim_rail_v (no-load rail, 3.3), sim_rail_ohm
    (output resistance, 0.05), sim_eta (converter efficiency 0..1, 0.9), sim_quiescent_ma (input current at no load, 1.0),
    sim_collapse_ma (the rail collapses to 0.4 V at and above this load, default never)."""
    v0 = to_float(p.get("sim_rail_v", 3.3), "sim_rail_v")
    r = to_float(p.get("sim_rail_ohm", 0.05), "sim_rail_ohm")
    eta = to_float(p.get("sim_eta", 0.9), "sim_eta")
    iq = to_float(p.get("sim_quiescent_ma", 1.0), "sim_quiescent_ma") / 1e3
    col = p.get("sim_collapse_ma")
    col = None if col is None else to_float(col, "sim_collapse_ma") / 1e3
    orig = fr._levels

    def rail(i):
        return 0.4 if (col is not None and i >= col - 1e-12) else max(0.0, v0 - r * i)

    def load_a():
        return fr._sink_a(2) if fr.st[2]["out"] else 0.0

    def levels(c):
        if c == 2:
            if fr.st[2]["out"] and fr.st[2]["emul"] in ("VMET", "VMETER"):     # voltmeter emulation reads the rail
                return (rail(0.0) if fr.st[1]["out"] else 0.0), 0.0
            if not fr.st[1]["out"] or not fr.st[2]["out"] and not getattr(fr, "off_reads_rail", False):
                return orig(c)
            i = load_a()
            return rail(i), (-i if i > 0 else 0.0)
        if c == 1 and fr.st[1]["out"]:
            i = load_a()
            v = fr.st[1]["v"]
            return v, iq + (rail(i) * i / (eta * v) if v > 0 else 0.0)
        return orig(c)
    fr._levels = levels


def need_lib_feature(fr, what: str) -> None:
    if not hasattr(fr, what):
        raise PluginError(f"the installed polystage-bench library is too old for this action (no {what}): update it")


def protection_gate(fr, ctx, may_clear: bool, refuse: bool = False) -> None:
    """Report a protection that is still latched on the frame, in plain words, before anything is energised.
    If and only if it is the output watchdog alone (PROT on every channel that shows one, protection coupling off, every
    output OFF) and `may_clear` is set, clear it (OUTP:PROT:CLE; energises nothing). Anything else is refused: the board
    is checked first and the latch cleared on the front panel. With may_clear False it warns, or with `refuse` raises (an
    action that would energise but is not allowed to clear)."""
    if not hasattr(fr, "protection_report"):
        return
    rep = fr.protection_report()
    if not rep["latched"]:
        return
    if not may_clear:
        if refuse:
            why = ("the output watchdog expired (a previous hold window ended). Run n6705b.first_power, which clears a "
                   "watchdog latch, or clear it on the front panel" if rep["watchdog_only"] else
                   "clear it on the front panel after checking the board")
            raise PluginError(f"a protection is latched on the frame: {rep['words']}. Nothing was energised. {why}.")
        ctx.log(f"a protection is latched on the frame: {rep['words']}. Readings may be invalid.", "warn")
        return
    if rep["watchdog_only"]:
        res = fr.clear_protection()
        if not res.get("cleared"):
            raise PluginError(f"a protection is latched on the frame ({rep['words']}) and could not be cleared: clear it "
                              "on the front panel after checking the board.")
        ctx.log("the previous hold window expired (watchdog); cleared")
        return
    raise PluginError(f"a protection is latched on the frame: {rep['words']}. Nothing was energised. Clear it on the "
                      "front panel after checking the board.")


class _N67Base:
    capabilities = ["dc_source"]
    name = ""
    simulate = os.environ.get("POLYBENCH_SIMULATE") == "1"

    def safe_state(self) -> str:
        """Open a fresh session, switch every output off and read it back. Never raises."""
        try:
            last = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        except (OSError, ValueError):
            last = {}
        try:
            _, profiles, _ = _lib()
            fr = open_frame(last, bool(last.get("simulate")) or self.simulate, profiles.Profile.safe_default())
            try:
                res = fr.output_off()
            finally:
                fr.close()
            if res.get("all_off"):
                return f"outputs off, read back OUTP? = {res['outputs']}"
            return f"NOT VERIFIED OFF: {res}"
        except Exception as e:
            return f"could not reach the power analyzer to confirm outputs are off: {e}. Check the front panel."

    @staticmethod
    def _off(fr, ctx) -> bool:
        """Switch every output off and read it back; True only when the readback says OFF."""
        try:
            res = fr.output_off()
            ctx.log(f"outputs off, read back {res.get('outputs')}" + ("" if res.get("all_off") else " NOT VERIFIED"),
                    "info" if res.get("all_off") else "error")
            return bool(res.get("all_off"))
        except Exception as e:
            ctx.log(f"could not confirm outputs off: {e}. Check the front panel.", "error")
            return False

    def selftest(self) -> bool:
        try:
            lib, profiles, _ = _lib()
        except PluginError:
            return False
        fr = lib.open_frame(backend="simulator", profile=profiles.Profile.safe_default())
        try:
            return "N67" in fr.idn_str.upper() or "KEYSIGHT" in fr.idn_str.upper() or bool(fr.channels)
        finally:
            fr.close()


class N6705bIdentify(_N67Base):
    """Read-only: *IDN? and the module inventory. map: {idn, serial} -> test ids (text)."""

    name = "n6705b.identify"

    def run(self, ctx) -> None:
        p = ctx.params
        _remember(p, ctx.simulate)
        fr = open_frame(p, ctx.simulate, load_profile(p, ctx.simulate, False))
        try:
            idn = fr.idn_str
            mods = {c: f"{i.get('model')} {i.get('serial')}" for c, i in fr.channels.items()}
            ctx.log(f"IDN: {idn}")
            ctx.log(f"modules: {mods}")
            mp = p.get("map") or {}
            if mp.get("idn"):
                ctx.propose(mp["idn"], idn, None, None, idn)
            if mp.get("serial"):
                ctx.propose(mp["serial"], idn.split(",")[2] if idn.count(",") >= 2 else idn, None, None, idn)
        finally:
            fr.close()
        ctx.progress(1.0, "done")


class N6705bMeasure(_N67Base):
    """Scalar V and I on one channel, averaged over `samples`. Does not change any output.

    params: channel (1), samples (5), current_unit ("mA"), map {voltage, current, power} -> test ids,
    output (omitted | "off" | "voltmeter"), profile (needed for "voltmeter").
    Omitted: the channel is read as it is; a reading taken with the output OFF is flagged in the log.
    "off": read a channel that is OFF without enabling it, refused if it is ON. An OFF N6781A reads 0 V on this frame
    (19 Oct, firmware D.02.13), so a reading below 0.05 V is refused as invalid: use "voltmeter".
    "voltmeter": the channel is put in voltmeter emulation (EMUL VMET, read back, OUTP ON, read, OUTP OFF, read back) and
    reads the rail it is wired to. It sources nothing and has no current reading. Needs `voltmeter = true` (or mode VMET)
    on that channel in the profile, and the channel must be OFF. Proposes only `map.voltage`.
    """

    name = "n6705b.measure"

    def run(self, ctx) -> None:
        p = ctx.params
        ch = to_int(p.get("channel", 1), "channel")
        n = max(1, min(100, to_int(p.get("samples", 5), "samples")))
        cu = _units(p)
        want = p.get("output")
        if want not in (None, "off", "voltmeter"):
            raise ParamError("output must be omitted, 'off' or 'voltmeter'")
        _remember(p, ctx.simulate)
        profile = load_profile(p, ctx.simulate, want == "voltmeter")
        fr = open_frame(p, ctx.simulate, profile)
        energised = False
        try:
            protection_gate(fr, ctx, may_clear=False)
            if want in ("off", "voltmeter") and ch not in fr.channels:
                raise PluginError(f"channel {ch} does not exist on this frame (has {sorted(fr.channels)})")
            if want == "voltmeter":
                need_lib_feature(fr, "voltmeter_read")
                mp = p.get("map") or {}
                if mp.get("current") or mp.get("power"):
                    ctx.log("voltmeter mode has no current reading: map.current and map.power are not proposed", "warn")
                if ctx.simulate and ch in fr.MODELS:
                    other = 1 if ch != 1 else 2
                    fr.st[other]["out"] = 1            # simulator: something else powers the rail
                    fr.rail_off_v = to_float(p.get("sim_rail_v", 3.3), "sim_rail_v")
                energised = True
                res = fr.audit([ch], fr.voltmeter_read, ch, n)
                for r in res.get("reasons") or []:
                    ctx.log(f"voltmeter read: {r}", "warn")
                ctx.log(f"ch{ch} in voltmeter mode: V={res['v_mean']:.5g} V (min {res['v_min']:.5g}, max {res['v_max']:.5g}, "
                        f"{res['n']} readings); output read back OFF")
                if mp.get("voltage"):
                    ctx.propose(mp["voltage"], fmt_num(res["v_mean"], 5), "V", None, f"voltmeter mode mean of {res['n']} readings")
                ctx.progress(1.0, "done")
                return
            if want == "off":
                if fr.measure(ch).get("output"):
                    raise PluginError(f"channel {ch} is ON: output 'off' reads a channel that is off. Nothing was changed. "
                                      "Leave 'output' out to read a live channel.")
            elif ctx.simulate and ch in fr.MODELS:      # a simulated rail so the numbers are not zero
                fr.load_ohm[ch] = 3.3 / (to_float(p.get("sim_current_ma", 20.0), "sim_current_ma") / 1e3)
                fr.set_output(ch, 3.3, 0.1, 4.0)
            rs = _read_samples(fr, ch, n, ctx)
            if want == "off" and rs[-1].get("output"):
                raise PluginError(f"channel {ch} turned ON during the read: something else is driving this frame")
        except BaseException:
            if energised:
                self._off(fr, ctx)
            raise
        finally:
            fr.close()
        vs, is_ = [r["v"] for r in rs], [r["i"] for r in rs]
        if want == "off":
            if _mean([abs(v) for v in vs]) < 0.05:
                raise PluginError("OFF channels read 0 V on this frame: use output: voltmeter. (An OFF N6781A does not read its "
                                  "terminals; the 0 V is not a measurement and was not proposed.)")
            ctx.log(f"ch{ch} is OFF (not enabled by this action) and reads {_mean(vs):.5g} V: a non-zero value means something "
                    "other than this channel drives the terminals; an OFF N6781A normally reads 0 V on this frame", "warn")
        elif not rs[-1].get("output"):
            ctx.log(f"channel {ch} output is OFF: the readings are not DUT supply values", "warn")
        ctx.log(f"ch{ch}: V={_mean(vs):.5g} V  I={_mean(is_) * CURRENT_SCALE[cu]:.5g} {cu}  ({n} readings, "
                f"{rs[-1].get('regulation')}, range {(rs[-1].get('range') or {}).get('range_a')} A)")
        ilim_m = to_float(p["ilim_a"], "ilim_a") if p.get("ilim_a") is not None else None     # a read does not know the limit unless told
        ctx.analysis(supply_analysis(rs, cu, ilim_m, tripped=False), _sample_rows(ch, rs, cu, 0.0))
        _propose_vi(ctx, p.get("map") or {}, vs, is_, cu)
        ctx.progress(1.0, "done")


class N6705bFirstPower(_N67Base):
    """Bring a DUT up on the profile's limits and read V and I.

    params: profile (required), channel (1), volts (required), ilim_a (optional, may only LOWER the profile's limit),
    ramp {start_v (default min(volts, 1.0)), step_v (0.25), dwell_s (0.2)}, settle_s (1.0), samples (5),
    leave "off" (default) | "on", current_unit ("mA"), map {voltage, current, power} -> test ids,
    sim_current_ma (simulator load, default 20).
    The OVP is set to the lower of the profile's ovp_max_v and 15 % (at least 0.3 V) over the setpoint.
    If a protection trips or any step fails, every output is switched off and the error is raised.
    """

    name = "n6705b.first_power"

    def run(self, ctx) -> None:
        p = ctx.params
        ch = to_int(p.get("channel", 1), "channel")
        volts = to_float(need(p, "volts"), "volts")
        n = max(1, min(100, to_int(p.get("samples", 5), "samples")))
        leave = p.get("leave", "off")
        if leave not in ("off", "on"):
            raise ParamError("leave must be 'off' or 'on'")
        cu = _units(p)
        ramp = p.get("ramp") or {}
        start_v = to_float(ramp.get("start_v", min(volts, 1.0)), "ramp.start_v")
        step_v = to_float(ramp.get("step_v", 0.25), "ramp.step_v")
        dwell = to_float(ramp.get("dwell_s", 0.2), "ramp.dwell_s")
        settle = to_float(p.get("settle_s", 1.0), "settle_s")
        if not 0 < volts <= 30 or not 0 < start_v <= volts or step_v <= 0:
            raise ParamError("volts / ramp values are not sensible")
        profile = load_profile(p, ctx.simulate, True)
        _remember(p, ctx.simulate)
        fr = open_frame(p, ctx.simulate, profile)
        energised = False
        try:
            if ch not in fr.channels:
                raise PluginError(f"channel {ch} does not exist on this frame (has {sorted(fr.channels)})")
            protection_gate(fr, ctx, may_clear=True)
            lim = profile_limits(fr, profile, ch)
            if volts > lim["max_volts"] + 1e-9:
                raise PluginError(f"{volts:g} V is above the profile limit {lim['max_volts']:g} V "
                                  f"(profile {profile.name}). Not energised.")
            ilim = lim["max_current_a"]
            if p.get("ilim_a") is not None:
                want = to_float(p["ilim_a"], "ilim_a")
                if want > ilim + 1e-12:
                    raise PluginError(f"ilim_a {want:g} A is above the profile limit {ilim:g} A. Not energised.")
                ilim = want
            ovp = min(lim["ovp_max_v"], max(volts * 1.15, volts + 0.3))
            if ovp <= volts:
                raise PluginError(f"profile OVP limit {lim['ovp_max_v']:g} V leaves no room above {volts:g} V")
            ctx.log(f"profile {profile.name}: ch{ch} {volts:g} V, current limit {ilim * 1e3:g} mA, OVP {ovp:g} V")
            if leave == "on":
                wd = lim.get("watchdog_s") or getattr(fr, "watchdog_s", None)
                ctx.log(f"leave=on: outputs switch off by themselves at {wd:g} s without commands; the frame shows a watchdog "
                        "protection afterwards" if wd else "leave=on: the frame watchdog is disabled, the output stays on until switched off",
                        "warn")
            if ctx.simulate and ch in fr.MODELS:
                fr.load_ohm[ch] = volts / (to_float(p.get("sim_current_ma", 20.0), "sim_current_ma") / 1e3)
            if ctx.cancelled():
                raise PluginError("cancelled")
            ctx.progress(0.1, f"energising at {start_v:g} V")
            energised = True
            fr.audit([ch], fr.set_output, ch, start_v, ilim, ovp)
            ramp_res, ramp_rs, ramp_rows = {}, [], []
            if volts > start_v + 1e-9:
                ctx.progress(0.4, f"ramping to {volts:g} V")
                # one library step at a time, so the current and the CV/CC mode are read at every step (the P06 evidence);
                # the same adjust_output call the library makes for a whole ramp, with the dwell kept here
                targets = []
                v_ = start_v
                while v_ + step_v < volts - 1e-9:
                    v_ += step_v
                    targets.append(round(v_, 6))
                targets.append(volts)
                t_ramp = 0.0
                try:
                    for tv in targets:
                        ramp_res = fr.adjust_output(ch, volts=tv, step_v=step_v, step_dwell_s=0.0) or {}
                        _cwait(fr, ctx, dwell)
                        t_ramp += dwell
                        m_ = fr.measure(ch)
                        ramp_rs.append(m_)
                        ramp_rows += [{**{k_: v2 for k_, v2 in r_.items() if k_ != "map_key"}, "quantity": ("ramp " + r_["quantity"]), "t_s": round(t_ramp, 3)} for r_ in _sample_rows(ch, [m_], cu, 0.0)]
                except Exception as e:
                    if type(e).__name__ == "ProtectionTrip":           # the library switched everything off; tell the protocol before the error
                        ctx.analysis(supply_analysis([], cu, ilim, trips=[str(e)], tripped=True, ramp_rs=ramp_rs), ramp_rows)
                    raise
            if ctx.cancelled():
                raise PluginError("cancelled")
            fr._wait(settle)
            ctx.progress(0.7, "measuring")
            rs = _read_samples(fr, ch, n, ctx)
            trips = fr.check_trips(report=True)
            sm = ramp_rows + _sample_rows(ch, rs, cu, round(ramp_steps_s(volts, start_v, step_v, dwell) + settle, 3))
            ctx.analysis(supply_analysis(rs, cu, ilim, trips=trips, ramp=ramp_res, ramp_rs=ramp_rs), sm)
            if trips:
                raise PluginError(f"protection tripped: {trips}")
            vs, is_ = [r["v"] for r in rs], [r["i"] for r in rs]
            reg = rs[-1].get("regulation")
            ctx.log(f"ch{ch}: V={_mean(vs):.5g} V  I={_mean(is_) * CURRENT_SCALE[cu]:.5g} {cu}  regulation {reg}")
            if reg == "CC":
                ctx.log("the supply is in current limit: the DUT draws more than the limit. Do not trust the voltage.", "warn")
            _propose_vi(ctx, p.get("map") or {}, vs, is_, cu)
            if leave == "on":
                ctx.log("leave=on: the output stays on until the frame watchdog (profile watchdog_s) expires or "
                        "n6705b.first_power / safe_state switches it off", "warn")
        except BaseException:
            if energised:
                self._off(fr, ctx)
            raise
        else:
            if leave == "off":
                self._off(fr, ctx)
        finally:
            fr.close()
        ctx.progress(1.0, "done")


class N6705bLoadSweep(_N67Base):
    """Step a constant-current load on a DUT rail and record rail voltage, input V and I and efficiency at each point.

    Channel `source_channel` (1) powers the DUT input, channel `load_channel` (2) is a constant-current load (CCL) on the
    DUT rail. Everything is judged against the profile before anything is energised; the sequence is source up, settle,
    read the rail through the load channel while it is OFF, refuse if the rail is above `guard_v`, then step the load.
    The N6781A cannot bound the sink in CCL (CURR:LIM:NEG answers +326), so the rail check, not the frame, is what keeps
    the load from pulling an overvoltage rail down.

    params: profile (required on real runs), source_channel (1), load_channel (2), volts (source, required),
    points_ma (ascending, first 0, required), dwell_s (0.5), samples (5, per point), settle_s (1.0, after the source is up),
    guard_v (default: the profile's voltage limit of the load channel; the load's voltage limit is set to it),
    stop_below_v (0.5: abort when the rail sags below it), max_total_s (60, never more), ilim_a (lower only),
    ramp {start_v, step_v, dwell_s} as first_power, current_unit ("mA"),
    map {rail_v_noload, rail_v_fullload, rail_v_min, rail_v_max, regulation_mv, input_ma_fullload, efficiency_pct_fullload, points_table}.
    points_table proposes every point as {columns, rows} (load, rail, input V, input I, efficiency).
    regulation_mv = no-load rail minus full-load rail, in mV. The last point is "full load".
    Efficiency = P_load / P_in per point, P_load = rail V x measured load current; empty at 0 mA.
    Aborts, with every output off and read back, on: rail below stop_below_v or above guard_v, any protection trip, the
    plan or the run exceeding max_total_s, link loss, cancel. The points go to a CSV and a JSON in the run folder (their path is
    the evidence of every proposal and is logged). Simulator: sim_rail_v, sim_rail_ohm, sim_eta, sim_quiescent_ma, sim_collapse_ma.
    """

    name = "n6705b.load_sweep"
    HARD_MAX_S = 60.0

    def run(self, ctx) -> None:
        p = ctx.params
        src = to_int(p.get("source_channel", 1), "source_channel")
        ld = to_int(p.get("load_channel", 2), "load_channel")
        volts = to_float(need(p, "volts"), "volts")
        pts = p.get("points_ma")
        if not isinstance(pts, list) or len(pts) < 2:
            raise ParamError("points_ma must be a list of at least two currents in mA, ascending, the first 0")
        pts = [to_float(x, "points_ma") for x in pts]
        if pts[0] != 0 or any(b <= a for a, b in zip(pts, pts[1:])) or len(pts) > 40:
            raise ParamError("points_ma must start at 0, be strictly ascending, and have at most 40 points")
        dwell = to_float(p.get("dwell_s", 0.5), "dwell_s")
        n = to_int(p.get("samples", 5), "samples")
        settle = to_float(p.get("settle_s", 1.0), "settle_s")
        stop_v = to_float(p.get("stop_below_v", 0.5), "stop_below_v")
        max_total = to_float(p.get("max_total_s", self.HARD_MAX_S), "max_total_s")
        if not 1 <= n <= 100 or dwell < 0 or settle < 0 or stop_v < 0:
            raise ParamError("samples 1..100, dwell_s, settle_s and stop_below_v must not be negative")
        if not 0 < max_total <= self.HARD_MAX_S:
            raise ParamError(f"max_total_s must be above 0 and at most {self.HARD_MAX_S:g}")
        if src == ld:
            raise ParamError("source_channel and load_channel must differ")
        cu = _units(p)
        ramp = p.get("ramp") or {}
        start_v = to_float(ramp.get("start_v", min(volts, 1.0)), "ramp.start_v")
        step_v = to_float(ramp.get("step_v", 0.25), "ramp.step_v")
        rdwell = to_float(ramp.get("dwell_s", 0.2), "ramp.dwell_s")
        if not 0 < volts <= 30 or not 0 < start_v <= volts or step_v <= 0 or rdwell < 0:
            raise ParamError("volts / ramp values are not sensible")
        mp = p.get("map") or {}
        profile = load_profile(p, ctx.simulate, True)
        _remember(p, ctx.simulate)
        fr = open_frame(p, ctx.simulate, profile)
        energised = False
        rows: list[dict] = []
        try:
            for c in (src, ld):
                if c not in fr.channels:
                    raise PluginError(f"channel {c} does not exist on this frame (has {sorted(fr.channels)})")
            # ---- every request against the profile, before anything is energised --------------------------------
            ls, ll = profile_limits(fr, profile, src), profile_limits(fr, profile, ld)
            protection_gate(fr, ctx, may_clear=False, refuse=True)
            need_lib_feature(fr, "voltmeter_read")
            try:
                vm_ok = profile.limits_for(ld).can_voltmeter
            except Exception:
                vm_ok = False
            if not vm_ok:
                raise PluginError(f"profile {profile.name}: channel {ld} must declare 'voltmeter = true': an OFF load channel reads "
                                  "0 V on the frame, so the rail is read in voltmeter mode. Not energised.")
            if ls["mode"] == "CCL":
                raise PluginError(f"profile {profile.name}: channel {src} is a load, it cannot be the source")
            if ll["mode"] != "CCL" or not ll["allow_sink"]:
                raise PluginError(f"profile {profile.name}: channel {ld} is not a constant-current load (mode CCL with "
                                  "allow_sink and max_sink_a). Not energised.")
            if volts > ls["max_volts"] + 1e-9:
                raise PluginError(f"{volts:g} V is above the profile limit {ls['max_volts']:g} V of channel {src} "
                                  f"(profile {profile.name}). Not energised.")
            ilim = ls["max_current_a"]
            if p.get("ilim_a") is not None:
                want = to_float(p["ilim_a"], "ilim_a")
                if want > ilim + 1e-12:
                    raise PluginError(f"ilim_a {want:g} A is above the profile limit {ilim:g} A. Not energised.")
                ilim = want
            ovp = min(ls["ovp_max_v"], max(volts * 1.15, volts + 0.3))
            if ovp <= volts:
                raise PluginError(f"profile OVP limit {ls['ovp_max_v']:g} V leaves no room above {volts:g} V")
            top_a = pts[-1] / 1e3
            if top_a > ll["max_sink_a"] + 1e-12:
                raise PluginError(f"{pts[-1]:g} mA is above the profile load limit {ll['max_sink_a'] * 1e3:g} mA of channel "
                                  f"{ld} (profile {profile.name}). Not energised.")
            guard = to_float(p["guard_v"], "guard_v") if p.get("guard_v") is not None else ll["max_volts"]
            if not 0 < guard <= ll["max_volts"] + 1e-9:
                raise PluginError(f"guard_v {guard:g} V is not within the profile voltage limit {ll['max_volts']:g} V of "
                                  f"channel {ld}. Not energised.")
            if stop_v >= guard:
                raise ParamError("stop_below_v must be below guard_v")
            ovp_l = min(ll["ovp_max_v"], max(guard * 1.15, guard + 0.3))
            if ovp_l < guard:
                raise PluginError(f"profile OVP limit {ll['ovp_max_v']:g} V of channel {ld} leaves no room above guard_v {guard:g} V")
            ramp_steps = math.ceil((volts - start_v) / step_v - 1e-9) if volts > start_v + 1e-9 else 0
            est = settle + ramp_steps * rdwell + 0.5 + len(pts) * (dwell + n * 0.1 + 0.4) + 2.0 + 2 * (n * 0.05 + 0.8)   # + two voltmeter reads
            if est > max_total + 1e-9:
                raise PluginError(f"the plan needs about {est:.0f} s (settle, {len(pts)} points x dwell and samples), above "
                                  f"max_total_s {max_total:g} s. Not energised: fewer points, shorter dwell or fewer samples.")
            ctx.log(f"profile {profile.name}: source ch{src} {volts:g} V (limit {ilim * 1e3:g} mA, OVP {ovp:g} V); load ch{ld} CCL, "
                    f"voltage limit {guard:g} V, {len(pts)} points to {pts[-1]:g} mA; plan about {est:.0f} s of {max_total:g} s")
            if ctx.simulate and ld in fr.MODELS:
                _sim_rail_model(fr, p)
            if ctx.cancelled():
                raise PluginError("cancelled")
            # ---- source up, settle, rail through the OFF load channel ---------------------------------------------
            t0 = fr._now()
            ctx.progress(0.05, f"energising the source at {start_v:g} V")
            energised = True
            fr.audit([src], fr.set_output, src, start_v, ilim, ovp)
            if volts > start_v + 1e-9:
                fr.adjust_output(src, volts=volts, step_v=step_v, step_dwell_s=rdwell)
            _cwait(fr, ctx, settle)
            # ---- the sweep. Point 0 is a voltmeter-mode read of the rail with the load channel OFF; it is also the guard ----
            load_on = False
            for k, ma in enumerate(pts):
                rail = None
                if ma == 0:
                    rail = self._guard(fr, ctx, ld, guard, t0, max_total, n)
                else:
                    sink = ma / 1e3
                    if not load_on:
                        try:
                            fr.refresh_limits(ld, "CCL")      # the change of emulation can have reset the output range
                        except Exception as e:
                            ctx.log(f"could not re-read the load channel limits: {e}", "warn")
                        vcap = fr.channels[ld].get("vmax")
                        try:
                            has_range = profile.limits_for(ld).output_range_v is not None
                        except Exception:
                            has_range = False
                        if vcap is not None and not has_range and guard > vcap + 1e-9:
                            raise PluginError(f"after voltmeter mode the load channel reports a voltage ceiling of {vcap:g} V, "
                                              f"below guard_v {guard:g} V: the load is NOT enabled, outputs off")
                        res = fr.audit([ld], fr.set_output, ld, guard, sink, ovp_l, sink_amps=sink, allow_sink=True)
                        load_on = True
                        for r in res.get("reasons") or []:
                            ctx.log(f"load channel: {r}", "warn")
                    else:
                        fr.adjust_output(ld, sink_amps=sink, allow_sink=True)
                _cwait(fr, ctx, dwell)
                ctx.progress(0.1 + 0.85 * (k + 1) / len(pts), f"{ma:g} mA")
                row = self._point(fr, ctx, src, ld, ma, n, rail)
                rows.append(row)
                trips = fr.check_trips(report=True)
                if trips:
                    raise PluginError(f"protection tripped: {trips}")
                if not fr.measure(src).get("output"):
                    raise PluginError("the source output went off during the sweep")
                if time_over(fr, t0, max_total):
                    raise PluginError(f"the sweep ran past max_total_s {max_total:g} s")
                if row["rail_v"] < stop_v:
                    raise PluginError(f"rail {row['rail_v']:.3f} V is below stop_below_v {stop_v:g} V at {ma:g} mA: sweep stopped, outputs off")
                if row["rail_v"] > guard:
                    raise PluginError(f"rail {row['rail_v']:.3f} V is above guard_v {guard:g} V at {ma:g} mA: sweep stopped, outputs off")
        except BaseException as e:
            off_ok = self._off(fr, ctx) if energised else None
            self._save(ctx, rows, cu, p, profile, complete=False)
            self._analysis(ctx, rows, cu, complete=False, why=str(e), planned=len(pts), outputs_off=off_ok)
            fr.close()
            raise
        # success: load off, source off, read back
        off_ok = self._off(fr, ctx)
        fr.close()
        path = self._save(ctx, rows, cu, p, profile, complete=True)
        self._analysis(ctx, rows, cu, complete=True, planned=len(pts), outputs_off=off_ok)
        self._propose(ctx, mp, rows, cu, path)
        ctx.progress(1.0, "done")

    @staticmethod
    def _guard(fr, ctx, ld, guard, t0, max_total, n) -> dict:
        """Read the rail in voltmeter mode on the (OFF) load channel; refuse above guard_v. An OFF N6781A reads 0 V on the
        frame, so a plain read of the OFF channel would always pass. Also the cancel / time / trip checks."""
        if ctx.cancelled():
            raise PluginError("cancelled")
        if time_over(fr, t0, max_total):
            raise PluginError(f"the sweep ran past max_total_s {max_total:g} s")
        res = fr.audit([ld], fr.voltmeter_read, ld, n)
        for r in res.get("reasons") or []:
            ctx.log(f"voltmeter read: {r}", "warn")
        if res["v_max"] > guard:
            raise PluginError(f"the rail reads {res['v_max']:.3f} V in voltmeter mode, above guard_v {guard:g} V: the load is "
                              "NOT enabled, outputs off")
        ctx.log(f"rail before the load is enabled: {res['v_mean']:.3f} V in voltmeter mode (guard {guard:g} V)")
        trips = fr.check_trips(report=True)
        if trips:
            raise PluginError(f"protection tripped: {trips}")
        return res

    @staticmethod
    def _point(fr, ctx, src, ld, ma, n, rail=None) -> dict:
        """One point: `n` readings. The rail comes from the load channel while the load is ON; at 0 mA (load off, which
        reads 0 V) it is the voltmeter-mode result passed in `rail`."""
        vr, il, vi, ii = [], [], [], []
        for i in range(n):
            if ctx.cancelled():
                raise PluginError("cancelled")
            s_ = fr.measure(src)
            if rail is None:
                r = fr.measure(ld)
                vr.append(r["v"]); il.append(abs(r["i"]) if r.get("output") else 0.0)
            vi.append(s_["v"]); ii.append(s_["i"])
            if i + 1 < n:
                fr._wait(0.1)
        if rail is not None:
            vr, il = [rail["v_mean"]], [0.0]
        row = {"set_ma": ma, "rail_v": _mean(vr), "load_a": _mean(il), "vin_v": _mean(vi), "iin_a": _mean(ii)}
        row["pin_w"] = row["vin_v"] * row["iin_a"]
        row["pout_w"] = row["rail_v"] * row["load_a"]
        row["eff_pct"] = (100.0 * row["pout_w"] / row["pin_w"]) if (ma > 0 and row["pin_w"] > 0) else None
        return row

    @staticmethod
    def _analysis(ctx, rows, cu, complete: bool, why: str = "", planned: int = 0, outputs_off: bool | None = None) -> None:
        """Every point as explorer rows (rail, input current, efficiency at each load) and the run's analysis block."""
        if not rows:
            ctx.analysis({"complete": complete, "points": 0, "tripped": "protection tripped" in why, "stopped_because": why or None,
                          "outputs_off": outputs_off, "sweep_ok": False}, [])
            return
        sc = CURRENT_SCALE[cu]
        samples = []
        for k, r in enumerate(rows):
            lab = f"{r['set_ma']:g} mA load"
            samples += [{"t_s": k, "channel": 0, "quantity": f"rail V at {lab}", "value": r["rail_v"], "unit": "V", "mode": None},
                        {"t_s": k, "channel": 0, "quantity": f"input I at {lab}", "value": r["iin_a"] * sc, "unit": cu, "mode": None}]
            if r["eff_pct"] is not None:
                samples.append({"t_s": k, "channel": 0, "quantity": f"efficiency at {lab}", "value": r["eff_pct"], "unit": "%", "mode": None})
        rails = [r["rail_v"] for r in rows]
        ctx.analysis({"complete": complete, "points": len(rows), "last_load_ma": rows[-1]["set_ma"], "rail": _stats(rails),
                      "tripped": "protection tripped" in why, "stopped_because": why or None, "outputs_off": outputs_off,
                      "settled": _settled(rails, rel=0.02, floor=0.005), "rail_collapsed": bool(why and "below stop_below_v" in why),
                      # P32: all planned points, no abort, no trip, both outputs read back OFF
                      "sweep_ok": bool(complete and len(rows) == planned and "protection tripped" not in why and outputs_off)}, samples)

    @staticmethod
    def _save(ctx, rows, cu, p, profile, complete: bool):
        if not rows:
            return None
        sc = CURRENT_SCALE[cu]
        try:
            d = Path(ctx.workdir)
            d.mkdir(parents=True, exist_ok=True)
            base = d / f"{safe_name(ctx.board)}_load_sweep_{stamp()}"
            cols = ["set_" + cu, "load_" + cu, "rail_V", "vin_V", "iin_" + cu, "pin_mW", "pout_mW", "efficiency_pct"]
            out = [[r["set_ma"] / 1e3 * sc, r["load_a"] * sc, r["rail_v"], r["vin_v"], r["iin_a"] * sc,
                    r["pin_w"] * 1e3, r["pout_w"] * 1e3, "" if r["eff_pct"] is None else r["eff_pct"]] for r in rows]
            with open(f"{base}.csv", "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(cols)
                w.writerows([[f"{x:.6g}" if isinstance(x, float) else x for x in row] for row in out])
            Path(f"{base}.json").write_text(json.dumps(
                {"action": "n6705b.load_sweep", "complete": complete, "profile": profile.name, "simulated": bool(ctx.simulate),
                 "columns": cols, "points": [dict(zip(cols, [None if x == "" else x for x in row])) for row in out]}, indent=1))
            ctx.log(f"points written: {base}.csv and {base}.json" + ("" if complete else " (sweep incomplete)"))
            ctx.artifact("n6705b_load_sweep", {"csv": f"{base}.csv", "json": f"{base}.json", "complete": complete})
            return f"{base}.csv"
        except OSError as e:
            ctx.log(f"could not write the sweep files: {e}", "error")
            return None

    @staticmethod
    def _propose(ctx, mp, rows, cu, path) -> None:
        sc = CURRENT_SCALE[cu]
        first, last = rows[0], rows[-1]
        rails = [r["rail_v"] for r in rows]
        ev = path
        vals = {
            "rail_v_noload": (first["rail_v"], "V"), "rail_v_fullload": (last["rail_v"], "V"),
            "rail_v_min": (min(rails), "V"), "rail_v_max": (max(rails), "V"),
            "regulation_mv": ((first["rail_v"] - last["rail_v"]) * 1e3, "mV"),
            "input_ma_fullload": (last["iin_a"] * sc, cu),
            "efficiency_pct_fullload": (last["eff_pct"], "%"),
        }
        if mp.get("points_table"):
            ctx.propose_table(mp["points_table"], [[r["set_ma"] / 1e3 * sc, r["rail_v"], r["vin_v"], r["iin_a"] * sc, r["eff_pct"]] for r in rows],
                              ["load_ma", "rail_v", "input_v", "input_ma", "efficiency_pct"], evidence=ev,
                              raw=f"load_sweep points, {len(rows)} rows")
        for key, (v, unit) in vals.items():
            if mp.get(key) and v is not None:
                ctx.propose(mp[key], fmt_num(v, 5), unit, ev, f"load_sweep {key}, {len(rows)} points, last {last['set_ma']:g} mA")


def time_over(fr, t0: float, limit: float) -> bool:
    return fr._now() - t0 > limit
