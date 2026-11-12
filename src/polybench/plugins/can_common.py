"""CAN helpers for the pcan.* actions: signal specs (from a DBC or from params), bit pack/unpack, the bus handle with a
simulator, statistics. No client data lives here: the DBC path, IDs and signal names always come in through params.

python-can and cantools are imported lazily, so the simulator and the unit tests need neither the adapter nor the driver.
"""
from __future__ import annotations

import math
import random
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._util import ParamError, PluginError, hex_bytes, to_float, to_int

MAX_DURATION_S = 600.0


@dataclass
class SigSpec:
    name: str
    can_id: int
    extended: bool
    start: int
    length: int
    little_endian: bool = True      # Intel (DBC @1); False = Motorola (DBC @0), start = MSB position
    signed: bool = False
    scale: float = 1.0
    offset: float = 0.0
    unit: str = ""
    is_float: bool = False
    message: str = ""

    def raw_to_phys(self, raw: float) -> float:
        return raw * self.scale + self.offset


@dataclass
class Frame:
    t: float
    can_id: int
    extended: bool
    data: bytes


# ---------------------------------------------------------------- bit packing (DBC conventions)

def _bit_positions(start: int, length: int, little: bool) -> list[int]:
    """Absolute bit indices (byte*8 + bit) from MSB to LSB of the signal."""
    if little:
        return [start + i for i in range(length - 1, -1, -1)]
    pos, b = [], start
    for _ in range(length):
        pos.append(b)
        byte, bit = divmod(b, 8)
        b = (byte + 1) * 8 + 7 if bit == 0 else b - 1
    return pos


def unpack_raw(data: bytes, start: int, length: int, little: bool, signed: bool) -> int:
    val = 0
    for b in _bit_positions(start, length, little):
        byte, bit = divmod(b, 8)
        if byte >= len(data):
            raise ValueError("signal outside the frame")
        val = (val << 1) | ((data[byte] >> bit) & 1)
    if signed and val >> (length - 1):
        val -= 1 << length
    return val


def pack_raw(buf: bytearray, start: int, length: int, little: bool, value: int) -> None:
    value &= (1 << length) - 1
    for i, b in enumerate(_bit_positions(start, length, little)):
        byte, bit = divmod(b, 8)
        if byte >= len(buf):
            raise ValueError("signal outside the frame")
        bitval = (value >> (length - 1 - i)) & 1
        buf[byte] = (buf[byte] & ~(1 << bit)) | (bitval << bit)


def decode_signal(s: SigSpec, data: bytes) -> tuple[int | float, float]:
    """(raw, physical). Float signals (DBC type float32/64) decode through struct."""
    if s.is_float:
        raw_i = unpack_raw(data, s.start, s.length, s.little_endian, False)
        fmt = {32: "I", 64: "Q"}[s.length]
        f = struct.unpack("<" + ("f" if s.length == 32 else "d"), struct.pack("<" + fmt, raw_i))[0]
        return f, f * s.scale + s.offset
    raw = unpack_raw(data, s.start, s.length, s.little_endian, s.signed)
    return raw, s.raw_to_phys(raw)


def encode_signals(specs: list[SigSpec], values: dict[str, float], dlc: int = 8) -> bytes:
    buf = bytearray(dlc)
    for s in specs:
        v = values.get(s.name, 0.0)
        if s.is_float:
            fmt = "f" if s.length == 32 else "d"
            raw = struct.unpack("<" + ("I" if s.length == 32 else "Q"), struct.pack("<" + fmt, (v - s.offset) / s.scale))[0]
        else:
            raw = int(round((v - s.offset) / s.scale))
            lo, hi = ((-(1 << (s.length - 1)), (1 << (s.length - 1)) - 1) if s.signed else (0, (1 << s.length) - 1))
            raw = max(lo, min(hi, raw))
        pack_raw(buf, s.start, s.length, s.little_endian, raw)
    return bytes(buf)


def encode_into(data: bytes, s: SigSpec, value: float) -> bytes:
    """Return `data` with signal `s` set to the physical `value`; other bits untouched."""
    buf = bytearray(data)
    if s.is_float:
        fmt = "f" if s.length == 32 else "d"
        raw = struct.unpack("<" + ("I" if s.length == 32 else "Q"), struct.pack("<" + fmt, (value - s.offset) / s.scale))[0]
    else:
        raw = int(round((value - s.offset) / s.scale))
        lo, hi = ((-(1 << (s.length - 1)), (1 << (s.length - 1)) - 1) if s.signed else (0, (1 << s.length) - 1))
        raw = max(lo, min(hi, raw))
    pack_raw(buf, s.start, s.length, s.little_endian, raw)
    return bytes(buf)


# ---------------------------------------------------------------- signal sets

class SignalSet:
    """The signals an action decodes: from `dbc` + `signals` (names), and/or an explicit `decode` list."""

    def __init__(self, specs: list[SigSpec]):
        self.specs = specs
        self.by_id: dict[tuple[int, bool], list[SigSpec]] = {}
        for s in specs:
            self.by_id.setdefault((s.can_id, s.extended), []).append(s)

    @property
    def names(self) -> list[str]:
        return [s.name for s in self.specs]

    def ids(self) -> list[tuple[int, bool]]:
        return list(self.by_id)

    def get(self, name: str) -> SigSpec:
        for s in self.specs:
            if s.name == name:
                return s
        raise ParamError(f"signal '{name}' is not in this action's signal list {self.names}")

    def decode_frame(self, f: Frame) -> dict[str, tuple[Any, float]]:
        out = {}
        for s in self.by_id.get((f.can_id, f.extended), []):
            try:
                out[s.name] = decode_signal(s, f.data)
            except (ValueError, KeyError):
                pass
        return out

    @classmethod
    def from_params(cls, params: dict) -> "SignalSet":
        specs: list[SigSpec] = []
        dbc = params.get("dbc")
        names = params.get("signals") or []
        if isinstance(names, str):
            names = [names]
        if dbc:
            specs += load_dbc_specs(str(dbc), names)
        elif names:
            raise ParamError("'signals' needs a 'dbc' path; or describe each signal in 'decode'")
        for d in params.get("decode") or []:
            try:
                specs.append(SigSpec(
                    name=str(d["name"]), can_id=to_int(d["id"], "decode.id"), extended=bool(d.get("extended", False)),
                    start=to_int(d["start"], "decode.start"), length=to_int(d["length"], "decode.length"),
                    little_endian=bool(d.get("little_endian", True)), signed=bool(d.get("signed", False)),
                    scale=to_float(d.get("scale", 1.0), "decode.scale"), offset=to_float(d.get("offset", 0.0), "decode.offset"),
                    unit=str(d.get("unit", "")), is_float=bool(d.get("float", False))))
            except KeyError as e:
                raise ParamError(f"decode entry {d!r} lacks {e}") from None
        seen = set()
        for s in specs:
            if s.name in seen:
                raise ParamError(f"signal name '{s.name}' appears twice")
            seen.add(s.name)
        return cls(specs)


def load_dbc_specs(path: str, names: list[str]) -> list[SigSpec]:
    try:
        import cantools
    except ImportError:
        raise PluginError("cantools is not installed for this Python: pip install cantools") from None
    p = Path(path).expanduser()
    if not p.is_file():
        raise ParamError(f"DBC not found: {p}")
    try:
        db = cantools.database.load_file(str(p), strict=False)
    except Exception as e:
        raise ParamError(f"cannot read DBC {p}: {e}") from None
    found: dict[str, SigSpec] = {}
    ambiguous: set[str] = set()
    for m in db.messages:
        for sg in m.signals:
            if sg.multiplexer_ids or sg.is_multiplexer:
                continue
            spec = SigSpec(name=sg.name, can_id=m.frame_id, extended=m.is_extended_frame, start=sg.start,
                           length=sg.length, little_endian=sg.byte_order == "little_endian", signed=sg.is_signed,
                           scale=float(sg.scale), offset=float(sg.offset), unit=sg.unit or "",
                           is_float=bool(sg.is_float), message=m.name)
            for key in (sg.name, f"{m.name}.{sg.name}"):
                if key in found and key == sg.name:
                    ambiguous.add(key)
                found.setdefault(key, spec)
    out = []
    for n in names:
        if n in ambiguous:
            raise ParamError(f"signal '{n}' is in more than one message; write it as Message.Signal")
        if n not in found:
            raise ParamError(f"signal '{n}' is not in {p.name}")
        s = found[n]
        out.append(SigSpec(**{**s.__dict__, "name": n}))
    return out


# ---------------------------------------------------------------- statistics (pure)

def series_stats(values: list[float], times: list[float] | None = None, full_scale: float | None = None) -> dict:
    """mean, sample std (= rms noise about the mean), peak-to-peak, rms value, n, effective rate; and, given the
    full-scale span, noise-free bits log2(FS/pp) and effective bits log2(FS/rms_noise) (ADS datasheet convention)."""
    n = len(values)
    if n == 0:
        raise PluginError("no samples to compute statistics from")
    mean = math.fsum(values) / n
    var = math.fsum((v - mean) ** 2 for v in values) / (n - 1) if n > 1 else 0.0
    std = math.sqrt(var)
    pp = max(values) - min(values)
    out = {"n": n, "mean": mean, "std": std, "rms_noise": std, "pp": pp, "min": min(values), "max": max(values),
           "rms": math.sqrt(math.fsum(v * v for v in values) / n), "last": values[-1]}
    if times and n > 1 and times[-1] > times[0]:
        out["rate_hz"] = (n - 1) / (times[-1] - times[0])
    if full_scale and full_scale > 0:
        out["full_scale"] = full_scale
        out["noise_free_bits"] = math.log2(full_scale / pp) if pp > 0 else None
        out["effective_bits"] = math.log2(full_scale / std) if std > 0 else None
    return out


def window_pp(values: list[float], window: int) -> dict:
    """Peak-to-peak over consecutive, non-overlapping windows of `window` samples (a trailing partial window is ignored).
    Returns the mean of the window values (the typical figure a datasheet quotes), the worst window, and how many windows.
    Needs at least one full window."""
    if window < 2:
        raise ParamError("pp window must be at least 2 samples")
    n_win = len(values) // window
    if n_win < 1:
        raise PluginError(f"only {len(values)} samples; a pp window of {window} needs at least {window}")
    pps = [max(values[i * window:(i + 1) * window]) - min(values[i * window:(i + 1) * window]) for i in range(n_win)]
    return {"pp_window": math.fsum(pps) / n_win, "pp_window_max": max(pps), "pp_windows": n_win, "pp_window_n": window}


def sequence_gaps(values: list[float], modulus: int) -> dict:
    """Lost frames from a counter that increments by one per frame and wraps at `modulus`.
    gaps = sum over consecutive pairs of ((delta mod modulus) - 1) for delta != 0; repeats = pairs with delta == 0
    (a duplicate, or exactly one full wrap lost, which cannot be told apart)."""
    if modulus < 2:
        raise ParamError("counter modulus must be at least 2")
    gaps = repeats = 0
    for a, b in zip(values, values[1:]):
        d = (int(round(b)) - int(round(a))) % modulus
        if d == 0:
            repeats += 1
        else:
            gaps += d - 1
    return {"seq_gaps": gaps, "seq_repeats": repeats, "seq_frames": len(values)}


# ---------------------------------------------------------------- bus handle

def _parse_filters(raw) -> list[dict]:
    out = []
    for f in raw or []:
        if isinstance(f, (int, str)):
            f = {"id": f}
        cid = to_int(f.get("id", f.get("can_id")), "filter.id")
        ext = bool(f.get("extended", cid > 0x7FF))
        mask = to_int(f.get("mask", 0x1FFFFFFF if ext else 0x7FF), "filter.mask")
        out.append({"can_id": cid, "can_mask": mask, "extended": ext})
    return out


class BusHandle:
    """One CAN adapter, real (python-can pcan) or simulated. Use as a context manager; closing always releases it."""

    def __init__(self, params: dict, simulate: bool, signals: SignalSet | None = None, sim_config: dict | None = None):
        self.params = params
        self.simulate = simulate
        self.signals = signals or SignalSet([])
        self.bitrate = to_int(params.get("bitrate", 1_000_000), "bitrate")
        self.channel = str(params.get("channel", "PCAN_USBBUS1"))
        self.listen_only = bool(params.get("listen_only", False))
        flt = _parse_filters(params.get("filters"))
        if not flt:   # default: only the IDs we decode
            flt = [{"can_id": i, "can_mask": 0x1FFFFFFF if e else 0x7FF, "extended": e} for i, e in self.signals.ids()]
        self.filters = flt
        self.bus = None
        self.sent: list[Frame] = []
        self._errors = 0
        self._sim = SimNode(self, sim_config or params.get("sim") or {}) if simulate else None
        self._t0 = time.monotonic()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    def open(self):
        if self.simulate:
            return
        try:
            import can
        except ImportError:
            raise PluginError("python-can is not installed for this Python: pip install python-can") from None
        import logging
        logging.getLogger("can").setLevel(logging.CRITICAL)
        kw: dict = dict(interface="pcan", channel=self.channel, bitrate=self.bitrate)
        if self.listen_only:
            kw["state"] = can.BusState.PASSIVE
        if self.filters:
            kw["can_filters"] = self.filters
        try:
            self.bus = can.Bus(**kw)
        except Exception as e:
            raise PluginError(explain_pcan_error(e, self.channel, self.bitrate)) from None
        time.sleep(0.2)  # PCBUSB settle after bring-up (the usual PCBUSB practice)

    def close(self):
        bus, self.bus = self.bus, None
        if bus is not None:
            try:
                bus.shutdown()
            except Exception:
                pass

    @property
    def error_frames(self) -> int:
        return self._sim.error_frames if self._sim else self._errors

    @property
    def reports_error_frames(self) -> bool:
        """python-can asks PCBUSB for error frames; the macOS PCBUSB library does not support that, so a count of 0 there
        proves nothing."""
        from .. import platform as _platform
        return True if self._sim else not _platform.IS_MACOS

    def now(self) -> float:
        return self._sim.t if self._sim else time.monotonic()

    def send(self, can_id: int, data: bytes, extended: bool = False) -> None:
        if self.listen_only:
            raise PluginError("listen_only is set: this action must not transmit")
        f = Frame(self.now(), can_id, extended, data)
        self.sent.append(f)
        if self._sim:
            self._sim.on_send(f)
            return
        import can
        try:
            self.bus.send(can.Message(arbitration_id=can_id, is_extended_id=extended, data=data), timeout=1.0)
        except Exception as e:
            raise PluginError(f"CAN send failed: {e}. Is a node on the bus (with termination) and the bitrate right?") from None

    def recv(self, timeout: float) -> Frame | None:
        if self._sim:
            return self._sim.next_frame(timeout)
        m = self.bus.recv(timeout=timeout)
        if m is not None and m.is_error_frame:
            self._errors += 1
        if m is None or m.is_error_frame or m.is_remote_frame:
            return None
        t = m.timestamp if m.timestamp and m.timestamp > 0 else time.monotonic()
        return Frame(t, m.arbitration_id, bool(m.is_extended_id), bytes(m.data))


def pcbusb_holders() -> list[str]:
    """Best effort: processes that have libPCBUSB loaded (they usually hold the channel). Never raises, 4 s limit.
    macOS and Linux only; on Windows there is no such list and the open attempt itself reports a busy channel."""
    from .. import platform as _platform
    return [f"pid {pid} ({label})" for pid, label in _platform.library_holders(["/usr/local/lib/libPCBUSB*.dylib"])]


def explain_pcan_error(e: Exception, channel: str, bitrate: int) -> str:
    s = f"{type(e).__name__}: {e}"
    low = s.lower()
    if "libpcbusb" in low or ("library" in low and "find" in low) or ("pcbusb" in low and "load" in low):
        why = "The PCBUSB driver (libPCBUSB.dylib from mac-can.com) is not installed."
    elif "nodriver" in low or "illhw" in low or "illhardware" in low or "illnet" in low or "no driver" in low:
        why = "No PCAN adapter found on this channel. Plug in the PCAN-USB and check the channel name."
    elif "not been initialized" in low or "initialization process has failed" in low or "busy" in low or "in use" in low:
        why = ("The PCAN channel could not be initialised: the adapter is not plugged in, or another program holds it "
               "(another CAN tool, a soak or capture script). Only one program can own the channel.")
        holders = pcbusb_holders()
        if holders:
            why += " Programs with the PCAN driver loaded: " + "; ".join(holders) + "."
    else:
        why = "Could not open the PCAN adapter."
    return f"{why} Channel {channel}, {bitrate} bit/s. ({s})"


# ---------------------------------------------------------------- simulator node

class SimNode:
    """Virtual CAN node. Periodic frames for the decoded signals and replies to request frames. Virtual time: no sleeping,
    so a 30 s capture takes milliseconds.

    sim config
      rate_hz   100   frames per second *per message* (messages are independent, each on its own schedule)
      rates     {signal name | "0x7D0": hz}   per-message override; a signal name selects the message it is in
      seed      1
      values    {signal: number | {mean, sd} | {counter: modulus, start?: n}}  physical values; a counter signal
                increments by one with every frame of its message (including dropped ones) and wraps at the modulus
      drop      [n, ...]  periodic-frame numbers (1-based, per message) that are not delivered; counters still advance,
                so the receiver sees a sequence gap
      error_frames  N     value the error-frame counter reads (a real PCBUSB on macOS never reports any)
      silent / query_only   no periodic frames
      reply     {request_id, id, data hex | values {...}, extended}   one reply (pcan.query)
      replies   [{request_id, request_data?, id, extended?, data hex}]   several; first whose request matches answers
    """

    def __init__(self, bus: BusHandle, cfg: dict):
        self.bus, self.cfg = bus, cfg
        self.t = 0.0
        self.rate = to_float(cfg.get("rate_hz", 100.0), "sim.rate_hz")
        self.rng = random.Random(to_int(cfg.get("seed", 1), "sim.seed"))
        self.values = {k: (v if isinstance(v, dict) else {"mean": v, "sd": 0.0}) for k, v in (cfg.get("values") or {}).items()}
        self.pending: list[Frame] = []
        self.periodic = not cfg.get("silent", False) and not cfg.get("query_only", False)
        self.drop = {to_int(n, "sim.drop") for n in (cfg.get("drop") or [])}
        self.error_frames = to_int(cfg.get("error_frames", 0), "sim.error_frames")
        ids = bus.signals.ids() or [(to_int(cfg.get("id", 0x7DF)), bool(cfg.get("extended", False)))]
        rates = {str(k): to_float(v, "sim.rates") for k, v in (cfg.get("rates") or {}).items()}
        self.msgs = []
        for i, ext in ids:
            specs = bus.signals.by_id.get((i, ext), [])
            hz = self.rate
            for key in [f"0x{i:X}", f"0x{i:x}", str(i)] + [s.name for s in specs]:
                if key in rates:
                    hz = rates[key]
            if hz <= 0:
                raise ParamError(f"sim rate for 0x{i:X} must be positive")
            self.msgs.append({"id": i, "ext": ext, "period": 1.0 / hz, "next": 0.0, "n": 0})

    def _find_reply(self, f: Frame) -> dict | None:
        cands = ([self.cfg["reply"]] if self.cfg.get("reply") else []) + list(self.cfg.get("replies") or [])
        for rep in cands:
            if to_int(rep.get("request_id", f.can_id), "sim.reply.request_id") != f.can_id:
                continue
            rd = rep.get("request_data")
            if rd is not None and not f.data.startswith(hex_bytes(rd, "sim.reply.request_data")):
                continue
            return rep
        return None

    def on_send(self, f: Frame) -> None:
        rep = self._find_reply(f)
        if rep is None:
            return
        ext = bool(rep.get("extended", False))
        rid = to_int(rep["id"], "sim.reply.id")
        if rep.get("data") is not None:
            data = hex_bytes(rep["data"])
        else:
            specs = self.bus.signals.by_id.get((rid, ext), [])
            vals = {s.name: float(self.values.get(s.name, {}).get("mean", 0.0)) for s in specs}
            data = encode_signals(specs, vals)
        self.pending.append(Frame(self.t + 0.002, rid, ext, data))

    def _value(self, name: str, count: int) -> float:
        v = self.values.get(name, {})
        if "counter" in v:
            return float((to_int(v.get("start", 0), "sim.counter.start") + count - 1) % to_int(v["counter"], "sim.counter"))
        return self.rng.gauss(float(v.get("mean", 0.0)), float(v.get("sd", 0.0)))

    def _frame(self, m: dict) -> Frame | None:
        m["n"] += 1
        t = m["next"]
        m["next"] += m["period"]
        if m["n"] in self.drop:
            return None
        specs = self.bus.signals.by_id.get((m["id"], m["ext"]), [])
        if specs:
            data = encode_signals(specs, {s.name: self._value(s.name, m["n"]) for s in specs})
        else:
            data = bytes([m["n"] & 0xFF]) + bytes(7)
        return Frame(t, m["id"], m["ext"], data)

    def next_frame(self, timeout: float) -> Frame | None:
        if self.pending:
            self.pending.sort(key=lambda f: f.t)
            f = self.pending.pop(0)
            self.t = max(self.t, f.t)
            return f
        if not self.periodic:
            self.t += timeout
            return None
        m = min(self.msgs, key=lambda x: x["next"])
        f = self._frame(m)
        if f is None:
            self.t = max(self.t, m["next"] - m["period"])
            return None
        self.t = max(self.t, f.t)
        return f
