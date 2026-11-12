"""Transports for the CH9329 cable: the real one over hidapi (optional extra `dmm`), and a simulator that replays scripted frames.

A transport moves 64-byte HID reports and nothing else. Framing and decoding live in frames.py. No code here can send a command other
than the two that frames.command_frame builds (Get Name, Get Measurement).
"""
from __future__ import annotations

import queue
import threading
import time

from . import frames as F

VID, PID = 0x1A86, 0xE429
INSTALL_HINT = "install the hidapi package: pip install -e '.[dmm]' (macOS: brew install hidapi only if the wheel lacks it)"


class TransportError(OSError):
    pass


class NotInstalled(TransportError):
    pass


def _hid():
    try:
        import hid
    except ImportError as e:
        raise NotInstalled("hidapi is not installed: " + INSTALL_HINT) from e
    return hid


def hid_available() -> tuple[bool, str]:
    try:
        _hid()
        return True, ""
    except NotInstalled as e:
        return False, str(e)


def find_cable() -> list[dict]:
    """CH9329 cables on USB by VID/PID. Enumeration only; the device is not opened. Raises NotInstalled without hidapi."""
    hid = _hid()
    try:
        return [{"path": d["path"], "product": d.get("product_string") or "", "serial": d.get("serial_number") or ""}
                for d in hid.enumerate(VID, PID)]
    except Exception as e:                                    # a hidapi failure is not a traceback in the server log
        raise TransportError(f"USB enumeration failed: {e}") from e


class HidTransport:
    name = "hid"

    def __init__(self, vid: int = VID, pid: int = PID):
        self.vid, self.pid = vid, pid
        self.dev = None

    def open(self) -> None:
        hid = _hid()
        d = hid.device()
        try:
            d.open(self.vid, self.pid)
        except Exception as e:
            raise TransportError(f"cannot open the cable ({e}); another program may hold it (only one process can)") from e
        self.dev = d

    def write(self, uart: bytes) -> None:
        if self.dev is None:
            raise TransportError("not open")
        try:
            n = self.dev.write(F.out_report(uart))
        except Exception as e:
            raise TransportError(f"write failed: {e}") from e
        if n is None or n < 0:
            raise TransportError("write failed")

    def read(self, timeout_ms: int) -> bytes:
        if self.dev is None:
            raise TransportError("not open")
        try:
            r = self.dev.read(64, timeout_ms)
        except Exception as e:
            raise TransportError(f"read failed: {e}") from e
        return bytes(r) if r else b""

    def close(self) -> None:
        d, self.dev = self.dev, None
        if d is not None:
            try:
                d.close()
            except Exception:
                pass


# ---- simulator ------------------------------------------------------------------------------------------------------------------
# Frames captured from the real UT161E on 27 Oct (brief section 3.4), replayed by the simulator and the tests.
CAPTURED = {
    "name_ack": bytes.fromhex("abcd04ff00027b"),
    "name": bytes.fromhex("abcd085554313631450306"),
    "ohm_ol": bytes.fromhex("abcd10063620204f4c2e2020040430303003a5"),
}

SCENES = {
    # name: (mode, range, display, flag bytes)
    "dc_v": (0x02, 1, "  5.007", (0x30, 0x30, 0x30)),
    "dc_mv": (0x03, 0, " 12.345", (0x30, 0x30, 0x30)),
    "ohm_ol": (0x06, 6, "  OL.  ", (0x30, 0x30, 0x30)),
    "ohm": (0x06, 1, "  4.700", (0x30, 0x30, 0x30)),
    "hold": (0x02, 1, "  5.007", (0x32, 0x30, 0x30)),
    "ac_v": (0x00, 1, "  3.300", (0x30, 0x30, 0x38)),
    "freq": (0x04, 0, "  1.000", (0x30, 0x30, 0x30)),
    "cap": (0x09, 0, "  4.700", (0x30, 0x30, 0x30)),
    "ma": (0x0E, 0, "  4.321", (0x30, 0x30, 0x30)),
}


class SimTransport:
    """A fake CH9329 cable plus meter. Scenes are the scripted frames; `value` makes a generated reading with +-1 count of noise.
    set_scene(name) / set_scene(mode=, range_idx=, display=, flags=); set_scene('unplugged') makes the cable disappear."""
    name = "sim"

    def __init__(self, latency_s: float = 0.05, split: bool = False, model: str = "UT161E", noise: bool = False):
        self.latency = latency_s
        self.split = split
        self.model = model
        self.noise = noise
        self.q: "queue.Queue[bytes]" = queue.Queue()
        self.is_open = False
        self.plugged = True
        self.scene = dict(zip(("mode", "range_idx", "display", "flags"), SCENES["dc_v"]))
        self.sent: list[int] = []               # command bytes received (tests assert only 5E / 5F ever arrive)
        self._lock = threading.Lock()
        self._tick = 0

    # control
    def set_scene(self, name: str | None = None, **kw) -> None:
        with self._lock:
            if name == "unplugged":
                self.plugged = False
                return
            if name == "plugged":
                self.plugged = True
                return
            self.plugged = True
            if name:
                self.scene = dict(zip(("mode", "range_idx", "display", "flags"), SCENES[name]))
            if "value" in kw and kw["value"] is not None:
                self.scene["display"] = str(kw.pop("value")).rjust(7)
            self.scene.update({k: v for k, v in kw.items() if v is not None})

    # transport
    def open(self) -> None:
        if not self.plugged:
            raise TransportError("simulated cable is unplugged")
        self.is_open = True

    def write(self, uart: bytes) -> None:
        if not self.plugged or not self.is_open:
            raise TransportError("simulated cable is unplugged")
        if len(uart) >= 4 and uart[:2] == F.HEADER:
            cmd = uart[3]
            self.sent.append(cmd)
            if cmd == F.CMD_GET_NAME:
                self._reply(bytes.fromhex("abcd04ff00027b"))
                self._reply(F.build_frame(self.model.encode("ascii")))
            elif cmd == F.CMD_GET_MEASUREMENT:
                with self._lock:
                    s = dict(self.scene)
                    self._tick += 1
                    tick = self._tick
                disp = s["display"]
                if self.noise and disp.strip().replace(".", "").replace("-", "").isdigit():
                    disp = _jitter(disp, tick)
                self._reply(F.build_measurement(s["mode"], s["range_idx"], disp, s["flags"]))

    def _reply(self, frame: bytes) -> None:
        parts = [frame[:7], frame[7:]] if self.split and len(frame) > 10 else [frame]
        for p in parts:
            self.q.put(bytes([len(p)]) + p)

    def read(self, timeout_ms: int) -> bytes:
        if not self.plugged:
            raise TransportError("simulated cable is unplugged")
        time.sleep(self.latency)                         # the meter answers in 40-75 ms on the real cable
        try:
            return self.q.get_nowait()
        except queue.Empty:
            time.sleep(min(timeout_ms, 50) / 1000)
            return b""

    def close(self) -> None:
        self.is_open = False
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break


def _jitter(disp: str, tick: int) -> str:
    """+-1 count on the last digit, deterministic."""
    t = disp.strip()
    neg = t.startswith("-")
    body = t.lstrip("-")
    if "." not in body:
        return disp
    dec = len(body.split(".")[1])
    from decimal import Decimal
    v = Decimal(body) + Decimal((tick % 3) - 1).scaleb(-dec)
    return (("-" if neg else "") + f"{v:.{dec}f}").rjust(7)
