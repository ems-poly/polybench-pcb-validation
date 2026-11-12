"""Actions `pcan.capture`, `pcan.stats`, `pcan.query`: python-can on a PEAK PCAN adapter (PCBUSB on macOS).

Opening the adapter is plain python-can: `can.Bus(interface="pcan", channel="PCAN_USBBUS1", bitrate=...)` and a
200 ms settle. Default bitrate is 1 Mbit/s (classic CAN), always overridable. The DBC path, IDs and
signal names come in through params; nothing is baked in. The actions never compute verdicts: they propose values.

Common params
  channel      "PCAN_USBBUS1"
  bitrate      1000000
  listen_only  false. True opens the adapter in listen-only mode and the action refuses to transmit. Note that a node
               with only the adapter on the bus needs the adapter to ACK, so leave it false unless watching a live bus.
  filters      [{id, mask?, extended?}] or [ids]. Default: the IDs of the decoded signals.
  dbc, signals DBC path and signal names (Message.Signal if a name is ambiguous), or
  decode       [{name, id, extended?, start, length, little_endian?, signed?, scale?, offset?, unit?, float?}]
  sim          simulator model (see can_common.SimNode): rate_hz, seed, values {signal: {mean, sd}}, reply {...}
"""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path

from ._util import ParamError, PluginError, fmt_num, hex_bytes, need, safe_name, stamp, to_float, to_int
from .can_common import MAX_DURATION_S, BusHandle, Frame, SignalSet, encode_into, sequence_gaps, series_stats, window_pp

STATS = ("last", "mean", "std", "rms_noise", "pp", "min", "max", "rms", "n", "rate_hz", "noise_free_bits", "effective_bits",
         "pp_window", "pp_window_max", "noise_free_bits_window", "seq_gaps", "seq_repeats", "error_frames")
COUNT_STATS = ("n", "pp_windows", "pp_window_n", "seq_gaps", "seq_repeats", "seq_frames", "error_frames")
BIT_STATS = ("noise_free_bits", "effective_bits", "noise_free_bits_window", "rate_hz")
VALUE_STATS = ("mean", "std", "rms_noise", "pp", "rms", "min", "max", "last", "pp_window", "pp_window_max")


class _PcanBase:
    capabilities = ["bus_adapter"]
    name = ""

    def safe_state(self) -> str:
        return "no CAN channel is held by this process (the adapter is released when the action ends); nothing is transmitting"

    def selftest(self) -> bool:
        from .can_common import pack_raw, unpack_raw
        buf = bytearray(8)
        pack_raw(buf, 4, 20, True, 0x5A5A5)
        pack_raw(buf, 55, 12, False, 0xABC)
        ok = unpack_raw(bytes(buf), 4, 20, True, False) == 0x5A5A5 and unpack_raw(bytes(buf), 55, 12, False, False) == 0xABC
        return ok and self._selftest()

    def _selftest(self) -> bool:
        return True


def _duration(p: dict, have_samples: bool) -> float:
    d = p.get("duration_s")
    if d is None:
        d = 30.0 if have_samples else 5.0
    d = to_float(d, "duration_s")
    if not 0 < d <= MAX_DURATION_S:
        raise ParamError(f"duration_s must be in (0, {MAX_DURATION_S:g}]")
    return d


def _send_preamble(ctx, bus: BusHandle) -> None:
    for item in ctx.params.get("send") or []:
        cid = to_int(need(item, "id"), "send.id")
        ext = bool(item.get("extended", cid > 0x7FF))
        data = hex_bytes(item.get("data", ""), "send.data")
        ctx.log(f"send 0x{cid:X}{' (ext)' if ext else ''} [{data.hex(' ')}]")
        bus.send(cid, data, ext)
        gap = to_float(item.get("delay_ms", 20), "send.delay_ms") / 1000.0
        if gap > 0 and not bus.simulate:
            time.sleep(min(gap, 2.0))
    settle = to_float(ctx.params.get("settle_ms", 200 if ctx.params.get("send") else 0), "settle_ms") / 1000.0
    if settle > 0 and not bus.simulate:
        time.sleep(min(settle, 5.0))


def gather(ctx, bus: BusHandle, sigs: SignalSet, duration: float, samples: int | None, discard: int = 0):
    """Receive until `duration` has passed or `samples` primary samples are in. Returns (rows, series, primary_name)."""
    rows: list[tuple] = []                  # (t, id, ext, data, {name: value})
    series: dict[str, tuple[list, list]] = {n: ([], []) for n in sigs.names}
    primary = sigs.names[0] if sigs.names else None
    skipped = 0
    t0 = bus.now()
    while True:
        elapsed = bus.now() - t0
        if ctx.cancelled():
            raise PluginError("cancelled")
        if elapsed >= duration:
            break
        if samples and primary and len(series[primary][0]) >= samples:
            break
        f = bus.recv(min(0.1, max(0.001, duration - elapsed)))
        if f is None:
            continue
        dec = sigs.decode_frame(f)
        if dec and primary in dec and skipped < discard:
            skipped += 1
            continue
        rows.append((f.t, f.can_id, f.extended, f.data, {n: v[1] for n, v in dec.items()}))
        for n, (raw, phys) in dec.items():
            series[n][0].append(f.t)
            series[n][1].append(phys)
        if len(rows) % 200 == 0:
            ctx.progress(min(0.95, elapsed / duration), f"{len(rows)} frames")
    return rows, series, primary


def write_capture_csv(path: Path, rows, names: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["t_s", "can_id", "extended", "dlc", "data_hex"] + names)
        t0 = rows[0][0] if rows else 0.0
        for t, cid, ext, data, vals in rows:
            w.writerow([f"{t - t0:.6f}", f"0x{cid:X}", int(ext), len(data), data.hex()] +
                       [repr(vals[n]) if n in vals else "" for n in names])


def _need_frames(rows, bus: BusHandle, sigs: SignalSet) -> None:
    if rows:
        return
    raise PluginError(
        "No CAN frames received. Check: node powered and running a CAN image; bitrate "
        f"({bus.bitrate} bit/s) matches the node; CAN_H/CAN_L wired; 120 ohm termination present at the adapter end "
        "(the node has none); IDs/filters " + (", ".join(f"0x{i:X}" for i, _ in sigs.ids()) or "(all)") + ".")


def extended_stats(st: dict, values: list[float], full_scale: float | None = None, window: int | None = None,
                   counter: list[float] | None = None, modulus: int | None = None,
                   bus: BusHandle | None = None, log=None) -> dict:
    """Adds to series_stats(): pp over `window`-sample windows (and the bits from it), sequence gaps of a counter series,
    and the adapter's error-frame count. Anything that cannot be computed is simply absent."""
    out = dict(st)
    if window:
        if len(values) >= window:
            w = window_pp(values, window)
            out.update(w)
            if full_scale and w["pp_window"] > 0:
                import math
                out["noise_free_bits_window"] = math.log2(full_scale / w["pp_window"])
        elif log:
            log(f"pp window {window} needs {window} samples, only {len(values)} decoded; pp_window not available", "warn")
    if counter is not None and modulus:
        out.update(sequence_gaps(counter, modulus))
    if bus is not None:
        if bus.reports_error_frames:
            out["error_frames"] = bus.error_frames
        elif log:
            log("error frames are not reported by the PCBUSB library on macOS: error_frames is not proposed "
                "(a count of 0 would prove nothing)", "warn")
    return out


def _fmt_stat(st: dict, key: str, scale: float, digits: int) -> str | None:
    v = st.get(key)
    if v is None:
        return None
    if key in COUNT_STATS:
        return str(int(v))
    if key in BIT_STATS:
        return fmt_num(v, digits)
    return fmt_num(v * scale, digits)


class PcanCapture(_PcanBase):
    """Capture frames for duration_s or `samples` decoded samples; save the raw CSV; propose decoded statistics.

    params: duration_s (5; 30 when `samples` given), samples, discard (first N samples dropped), send [{id,data,extended,
    delay_ms}], settle_ms, propose [{test, signal, stat: last|mean|std|pp|min|max|rms|n|rate_hz, unit?, scale?, digits?}].
    """

    name = "pcan.capture"

    def run(self, ctx) -> None:
        p = ctx.params
        sigs = SignalSet.from_params(p)
        samples = to_int(p["samples"], "samples") if p.get("samples") else None
        duration = _duration(p, samples is not None)
        props = p.get("propose") or []
        for pr in props:
            if pr.get("signal") not in sigs.names:
                raise ParamError(f"propose entry names signal {pr.get('signal')!r}, not in {sigs.names}")
            if pr.get("stat", "mean") not in STATS:
                raise ParamError(f"unknown stat {pr.get('stat')!r}; one of {STATS}")
        with BusHandle(p, ctx.simulate, sigs) as bus:
            ctx.progress(0.02, "adapter open" + (" (simulated)" if ctx.simulate else ""))
            _send_preamble(ctx, bus)
            rows, series, _prim = gather(ctx, bus, sigs, duration, samples, to_int(p.get("discard", 0), "discard"))
        _need_frames(rows, bus, sigs)
        path = Path(ctx.workdir) / f"{safe_name(ctx.board)}_capture_{stamp()}.csv"
        write_capture_csv(path, rows, sigs.names)
        ctx.log(f"{len(rows)} frames saved to {path}")
        for pr in props:
            n = pr["signal"]
            t, v = series[n]
            if not v:
                ctx.log(f"signal {n}: no samples decoded; nothing proposed for {pr['test']}", "warn")
                continue
            stat = pr.get("stat", "mean")
            fs = to_float(pr["full_scale"], "full_scale") if pr.get("full_scale") else None
            window = to_int(pr["window"], "window") if pr.get("window") else None
            modulus = to_int(pr["modulus"], "modulus") if pr.get("modulus") else None
            if stat in ("pp_window", "pp_window_max", "noise_free_bits_window") and not window:
                raise ParamError(f"stat '{stat}' needs 'window' (samples) in the propose entry for {pr['test']}")
            if stat == "noise_free_bits_window" and not fs:
                raise ParamError(f"stat '{stat}' needs 'full_scale' in the propose entry for {pr['test']}")
            if stat in ("seq_gaps", "seq_repeats") and not modulus:
                raise ParamError(f"stat '{stat}' needs 'modulus' (counter wraps at) in the propose entry for {pr['test']}")
            st = extended_stats(series_stats(v, t, fs), v, fs, window, v if modulus else None, modulus,
                                bus if stat == "error_frames" else None, ctx.log)
            val = _fmt_stat(st, stat, to_float(pr.get("scale", 1.0), "scale"), to_int(pr.get("digits", 6), "digits"))
            if val is None:
                ctx.log(f"{stat} of {n} not available; nothing proposed for {pr['test']}", "warn")
                continue
            unit = None if stat in COUNT_STATS or stat in BIT_STATS else pr.get("unit", next(s.unit for s in sigs.specs if s.name == n) or None)
            if stat == "rate_hz":
                unit = "Hz"
            if stat.startswith("noise_free_bits"):
                unit = "bit"
            ctx.propose(pr["test"], val, unit, str(path), f"{n} {stat} over {st['n']} samples")
        ctx.progress(1.0, "done")

    def _selftest(self) -> bool:
        return _sim_selftest()


class PcanStats(_PcanBase):
    """Noise and accuracy statistics of one decoded signal.

    params: signal (one name), full_scale (full-scale SPAN in the signal's unit, for the bits), samples / duration_s,
    discard, send, unit (label), display_scale (multiply the value-type stats, e.g. 1e6 for V -> uV),
    map {mean, std, rms_noise, pp, rms, min, max, n, rate_hz, noise_free_bits, effective_bits} -> test id.
    Definitions (ADS datasheet convention): rms_noise = sample std dev; noise_free_bits = log2(FS/pp);
    effective_bits = log2(FS/rms_noise). Raises if the signal is flat zero-noise (a dead source or a stuck code).
    """

    name = "pcan.stats"

    def run(self, ctx) -> None:
        p = ctx.params
        p = {**p, "signals": p.get("signals") or ([p["signal"]] if p.get("signal") and p.get("dbc") else [])}
        sigs = SignalSet.from_params(p)
        sig = need(p, "signal")
        sigs.get(sig)
        window = to_int(p["window"], "window") if p.get("window") else None
        ctr = p.get("counter")
        ctr_sig, modulus = None, None
        if ctr:
            ctr_sig = str(need(ctr, "signal"))
            modulus = to_int(need(ctr, "modulus"), "counter.modulus")
            if ctr_sig not in sigs.names:     # the counter may be a second signal of the same DBC
                sigs = SignalSet.from_params({**p, "signals": list(p["signals"]) + [ctr_sig]})
        samples = to_int(p["samples"], "samples") if p.get("samples") else None
        duration = _duration(p, samples is not None)
        fs = to_float(p["full_scale"], "full_scale") if p.get("full_scale") else None
        scale = to_float(p.get("display_scale", 1.0), "display_scale")
        digits = to_int(p.get("digits", 5), "digits")
        mp = p.get("map") or {}
        bad = [k for k in mp if k not in STATS]
        if bad:
            raise ParamError(f"unknown map keys {bad}; one of {STATS}")
        with BusHandle(p, ctx.simulate, sigs) as bus:
            ctx.progress(0.02, "adapter open" + (" (simulated)" if ctx.simulate else ""))
            _send_preamble(ctx, bus)
            rows, series, _ = gather(ctx, bus, sigs, duration, samples, to_int(p.get("discard", 0), "discard"))
        _need_frames(rows, bus, sigs)
        t, v = series[sig]
        if len(v) < 2:
            raise PluginError(f"only {len(v)} sample(s) of {sig} decoded; need at least 2 for statistics")
        st = series_stats(v, t, fs)
        want_err = "error_frames" in mp
        st = extended_stats(st, v, fs, window, series[ctr_sig][1] if ctr_sig else None, modulus,
                            bus if want_err else None, ctx.log)
        for k in ("pp_window", "pp_window_max", "noise_free_bits_window"):
            if k in mp and not window:
                ctx.log(f"{k} needs 'window' in the params; not proposed", "warn")
        for k in ("seq_gaps", "seq_repeats"):
            if k in mp and not ctr:
                ctx.log(f"{k} needs 'counter: {{signal, modulus}}' in the params; not proposed", "warn")
        unit = p.get("unit") or next(s.unit for s in sigs.specs if s.name == sig) or None
        base = Path(ctx.workdir) / f"{safe_name(ctx.board)}_stats_{stamp()}"
        base.parent.mkdir(parents=True, exist_ok=True)
        write_capture_csv(base.with_suffix(".csv"), rows, sigs.names)
        report = {"signal": sig, "unit": unit, "display_scale": scale, **{k: st[k] for k in st}}
        base.with_suffix(".json").write_text(json.dumps(report, indent=1))
        ctx.log(f"{sig}: n={st['n']} mean={st['mean']:.6g} std={st['std']:.4g} pp={st['pp']:.4g} "
                f"rate={st.get('rate_hz', float('nan')):.1f} Hz"
                + (f" NFB={st['noise_free_bits']:.2f} ENOB={st['effective_bits']:.2f}" if st.get("noise_free_bits") else ""))
        if st["std"] == 0:
            ctx.log("zero noise: the code never changed. Check the source is live; bits are not computed.", "warn")
        for key, test in mp.items():
            if key in ("noise_free_bits", "effective_bits", "noise_free_bits_window") and not fs:
                ctx.log(f"{key} needs full_scale in the params; not proposed", "warn")
                continue
            val = _fmt_stat(st, key, scale, digits)
            if val is None:
                ctx.log(f"{key}: not available; not proposed for {test}", "warn")
                continue
            u = None
            if key in VALUE_STATS:
                u = p.get("display_unit", unit)
            elif key == "rate_hz":
                u = "Hz"
            elif key.startswith(("noise_free_bits", "effective_bits")):
                u = "bit"
            ctx.propose(test, val, u, str(base.with_suffix(".json")), json.dumps({key: st.get(key)}))
        ctx.progress(1.0, "done")

    def _selftest(self) -> bool:
        s = series_stats([0.0, 1.0, 2.0, 3.0], [0, 1, 2, 3], 16.0)
        return abs(s["std"] - 1.2909944) < 1e-6 and s["pp"] == 3.0 and abs(s["noise_free_bits"] - 2.4150375) < 1e-6 and s["rate_hz"] == 1.0


class PcanQuery(_PcanBase):
    """Send request frames and wait for the replies (config-register reads, commands, bring-up images that answer).

    One query (flat params) or several (`queries: [...]`), all on one open adapter:

      params       channel, bitrate, listen_only, timeout_s (2), retries (0), dbc/signals/decode (shared by every query),
                   sim {...}
      query        request {id, data, extended} (omit to just wait for a frame), response {id, extended?, prefix?},
                   timeout_s, retries, decode / dbc+signals (added to the shared ones, for this query only),
                   propose [{test, signal, as: number|hex|yn, unit?, digits?}], sim {values: {signal: physical value}}
      `queries`    a list of query entries; the flat request/response/propose are then not used.

    Every query is tried even if an earlier one got no reply; the proposals of the ones that answered are kept and the
    action then fails, naming the queries without a reply. `as: hex` proposes the raw value as 0x..; `as: yn` proposes Y
    when the physical value is nonzero.

    Simulator: the node answers each request with a frame that starts with the response `prefix` bytes and carries the
    query's signals at their decoded bit positions, valued from `sim.values` (action level or query level, in physical units,
    default 0), so the reply decodes through the same DBC / decode map as a real one. An explicit sim.reply /
    sim.replies still wins.
    """

    name = "pcan.query"

    def run(self, ctx) -> None:
        p = ctx.params
        shared = SignalSet.from_params(p)
        queries = p.get("queries")
        if queries is None:
            queries = [{k: p[k] for k in ("request", "response", "propose", "decode", "timeout_s", "retries") if k in p}]
        elif not isinstance(queries, list) or not queries:
            raise ParamError("'queries' must be a non-empty list")
        plans = [self._plan(i, q, p, shared) for i, q in enumerate(queries)]
        union = SignalSet(list({(s.name, s.can_id): s for pl in plans for s in pl["sigs"].specs}.values()))
        simcfg = dict(p.get("sim") or {})
        if ctx.simulate:
            simcfg["silent"] = not any(pl["req"] is None for pl in plans)
            simcfg["values"] = {**(simcfg.get("values") or {}),
                                **{k: v for pl in plans for k, v in (pl["sim_values"] or {}).items()}}
            replies = list(simcfg.get("replies") or [])
            for pl in plans:
                if pl["req"] is not None and not pl["no_reply"]:
                    replies.append(self._sim_reply(pl, simcfg["values"]))
            simcfg["replies"] = replies
        q = {**p, "filters": p.get("filters") or [{"id": rid, "extended": rext}
                                                  for rid, rext in dict.fromkeys((pl["rid"], pl["rext"]) for pl in plans)]}
        failed: list[str] = []
        results = []
        with BusHandle(q, ctx.simulate, union, simcfg) as bus:
            for k, pl in enumerate(plans):
                ctx.progress(k / len(plans), pl["label"])
                got = self._ask(ctx, bus, pl)
                if got is None:
                    failed.append(pl["label"])
                    ctx.log(f"{pl['label']}: no reply with ID 0x{pl['rid']:X} within {pl['timeout']:g} s"
                            + (f" after {pl['retries'] + 1} attempts" if pl["retries"] else ""), "error")
                    continue
                ctx.log(f"{pl['label']}: reply 0x{got.can_id:X} [{got.data.hex(' ')}]")
                results.append((pl, got))
        for pl, got in results:
            self._propose(ctx, pl, got)
        if failed:
            first = plans[0]
            raise PluginError(
                (f"No reply with ID 0x{first['rid']:X} within {first['timeout']:g} s"
                 + (f" after {first['retries'] + 1} attempts" if first["retries"] else "")
                 if len(plans) == 1 else
                 f"No reply from {len(failed)} of {len(plans)} queries: {', '.join(failed)}")
                + ". Check the node is running the right image, bitrate and wiring.")
        ctx.progress(1.0, "done")

    @staticmethod
    def _plan(i: int, q: dict, p: dict, shared: SignalSet) -> dict:
        resp = need(q, "response", dict)
        rid = to_int(need(resp, "id"), "response.id")
        rext = bool(resp.get("extended", rid > 0x7FF))
        timeout = to_float(q.get("timeout_s", p.get("timeout_s", 2.0)), "timeout_s")
        if not 0 < timeout <= 30:
            raise ParamError("timeout_s must be in (0, 30]")
        own = SignalSet.from_params({k: q[k] for k in ("dbc", "signals", "decode") if k in q})
        sigs = SignalSet(shared.specs + own.specs)
        props = q.get("propose") or []
        for pr in props:
            if pr.get("signal") not in sigs.names:
                raise ParamError(f"propose entry names signal {pr.get('signal')!r}, not in {sigs.names}")
        req = q.get("request")
        label = str(q.get("title") or q.get("id") or (f"query {i + 1}" if "queries" in p else "query"))
        return {"req": req, "rid": rid, "rext": rext, "prefix": hex_bytes(resp.get("prefix", ""), "response.prefix"),
                "timeout": timeout, "retries": to_int(q.get("retries", p.get("retries", 0)), "retries"),
                "sigs": sigs, "props": props, "label": label, "sim_values": (q.get("sim") or {}).get("values"),
                "no_reply": bool((q.get("sim") or {}).get("no_reply"))}

    @staticmethod
    def _sim_reply(pl: dict, values: dict) -> dict:
        data = bytearray(pl["prefix"].ljust(8, b"\0")[:8])
        for s in pl["sigs"].by_id.get((pl["rid"], pl["rext"]), []):
            if s.name in {pr["signal"] for pr in pl["props"]}:
                v = values.get(s.name, 0.0)
                v = v.get("mean", 0.0) if isinstance(v, dict) else v
                data[:] = encode_into(bytes(data), s, float(v))
        req = pl["req"]
        return {"request_id": to_int(req["id"], "request.id"), "request_data": req.get("data", "") or None,
                "id": pl["rid"], "extended": pl["rext"], "data": bytes(data).hex()}

    @staticmethod
    def _ask(ctx, bus: BusHandle, pl: dict):
        req = pl["req"]
        for attempt in range(pl["retries"] + 1):
            if req:
                bus.send(to_int(req["id"], "request.id"), hex_bytes(req.get("data", ""), "request.data"),
                         bool(req.get("extended", to_int(req["id"]) > 0x7FF)))
            t_end = bus.now() + pl["timeout"]
            while bus.now() < t_end:
                if ctx.cancelled():
                    raise PluginError("cancelled")
                f = bus.recv(0.05)
                if f and f.can_id == pl["rid"] and f.extended == pl["rext"] and f.data.startswith(pl["prefix"]):
                    return f
        return None

    @staticmethod
    def _propose(ctx, pl: dict, got: Frame) -> None:
        sigs = pl["sigs"]
        dec = sigs.decode_frame(got)
        path = Path(ctx.workdir) / f"{safe_name(ctx.board)}_query_{stamp()}_{safe_name(pl['label'])}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"query": pl["label"], "id": got.can_id, "data": got.data.hex(),
                                    "decoded": {k: v[1] for k, v in dec.items()}, "raw": {k: v[0] for k, v in dec.items()}}, indent=1))
        for pr in pl["props"]:
            n = pr["signal"]
            if n not in dec:
                ctx.log(f"signal {n} not in the reply frame; nothing proposed for {pr['test']}", "warn")
                continue
            raw, phys = dec[n]
            kind = pr.get("as", "number")
            if kind == "hex":
                val, unit = f"0x{int(raw) & 0xFFFFFFFF:X}", None
            elif kind == "yn":
                val, unit = ("Y" if phys else "N"), None
            else:
                val = fmt_num(phys, to_int(pr.get("digits", 6), "digits"))
                unit = pr.get("unit", next(s.unit for s in sigs.specs if s.name == n) or None)
            ctx.propose(pr["test"], val, unit, str(path), got.data.hex())

    def _selftest(self) -> bool:
        return True


def _sim_selftest() -> bool:
    """Capture through the simulator with explicit decode entries; needs no hardware, no DBC."""
    sigs = SignalSet.from_params({"decode": [{"name": "x", "id": 0x100, "start": 0, "length": 24, "signed": True,
                                              "scale": 1e-6, "unit": "V"}]})

    class _Ctx:
        def cancelled(self): return False
        def progress(self, *a, **k): pass
    with BusHandle({}, True, sigs, {"rate_hz": 200, "values": {"x": {"mean": 0.001, "sd": 1e-5}}}) as bus:
        rows, series, _ = gather(_Ctx(), bus, sigs, 1.0, None)
    t, v = series["x"]
    st = series_stats(v, t)
    return 150 < len(v) <= 210 and abs(st["mean"] - 0.001) < 5e-6
