"""One owner of the cable: MeterLink does the request/response work on an open transport; MeterReader is the background thread
the server runs; read_stable() is what the `dmm.read` action uses when it holds the device itself.

States (MeterReader.state):  not_installed | no_cable | silent | connected | lost
  no_cable   hidapi is there but no CH9329 cable on USB
  silent     the cable opens but the meter does not answer (meter off, auto power-off, or its USB function is off)
  lost       it was connected and the answers stopped (cable pulled, meter off): shown within LOST_AFTER_S
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable

from . import frames as F
from . import reading as R
from .stability import StabilityWindow
from .transport import NotInstalled, TransportError, find_cable

log = logging.getLogger("polybench.meter")

LOST_AFTER_S = 1.5          # no valid frame for this long = lost (R12: shown within 2 s)
ANSWER_TIMEOUT_S = 0.4      # one request: wait this long for a frame
NAME_TRIES = 3


class MeterLink:
    """Request/response over an open transport. Sends only Get Name and Get Measurement."""

    def __init__(self, transport):
        self.t = transport
        self.buf = F.FrameBuffer()
        self.frames_ok = 0
        self.frames_bad = 0         # checksum / structure failures
        self.frames_wrong = 0       # valid frames of the wrong kind or length for the request
        self.model = ""

    @property
    def bad_total(self) -> int:
        return self.buf.bad + self.frames_bad + self.frames_wrong

    def _collect(self, timeout_s: float, want) -> list[bytes]:
        """Read reports until `want(frames)` is satisfied or the time is up. Returns the valid frames seen."""
        end = time.monotonic() + timeout_s
        out: list[bytes] = []
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return out
            rep = self.t.read(max(1, int(left * 1000)))
            if rep:
                out += self.buf.feed(F.in_report_payload(rep))
                if want(out):
                    return out

    def get_name(self, timeout_s: float = 0.6) -> str:
        self.t.write(F.command_frame(F.CMD_GET_NAME))
        for fr in self._collect(timeout_s, lambda fs: any(F.parse_name(f) for f in fs)):
            n = F.parse_name(fr)
            if n:
                self.model = n
                return n
        return ""

    def measure(self, timeout_s: float = ANSWER_TIMEOUT_S) -> "F.Measurement | None":
        """One Get Measurement. None when no valid measurement frame arrived in time (bad and wrong-length frames are counted)."""
        self.t.write(F.command_frame(F.CMD_GET_MEASUREMENT))
        got = self._collect(timeout_s, lambda fs: any(len(f) == F.MEAS_TOTAL for f in fs))
        for fr in got:
            if len(fr) != F.MEAS_TOTAL:
                self.frames_wrong += 1          # e.g. a late name answer
                continue
            try:
                m = F.parse_measurement(fr)
            except F.FrameError:
                self.frames_bad += 1
                continue
            self.frames_ok += 1
            return m
        return None


class MeterReader(threading.Thread):
    """Background poller. Reconnects by itself. Never raises into the server: every failure becomes a state and a reason."""

    def __init__(self, factory: Callable[[], object], on_reading: Callable, on_state: Callable[[], None] | None = None,
                 finder: Callable[[], list] | None = None, retry_s: float = 1.0):
        super().__init__(name="meter-reader", daemon=True)
        self.factory = factory
        self.finder = finder if finder is not None else find_cable
        self.on_reading = on_reading
        self.on_state = on_state or (lambda: None)
        self.retry_s = retry_s
        self.state = "no_cable"
        self.reason = ""
        self.model = ""
        self.cable = ""
        self.stop_ev = threading.Event()
        self._pause = threading.Event()
        self._paused_ack = threading.Event()
        self.link: MeterLink | None = None
        self.last_ok = 0.0
        self.since = time.monotonic()
        self.times: deque[float] = deque(maxlen=40)
        self.stats = {"ok": 0, "bad": 0, "wrong": 0, "timeouts": 0, "reconnects": 0}

    # ---- control
    def stop(self):
        self.stop_ev.set()

    def pause(self, wait_s: float = 3.0) -> bool:
        """Release the cable (for the dmm.read action) and wait until it is released."""
        self._paused_ack.clear()
        self._pause.set()
        return self._paused_ack.wait(wait_s)

    def resume(self):
        self._pause.clear()

    def rate_hz(self) -> float:
        t = list(self.times)
        return round((len(t) - 1) / (t[-1] - t[0]), 1) if len(t) >= 3 and t[-1] > t[0] else 0.0

    def status(self) -> dict:
        link = self.link
        return {"state": self.state, "reason": self.reason, "model": self.model, "cable": self.cable, "rate_hz": self.rate_hz(),
                "frames_ok": (link.frames_ok if link else 0), "frames_bad": self.stats["bad"] + (link.bad_total if link else 0),
                "reconnects": self.stats["reconnects"], "paused": self._pause.is_set()}

    def _set(self, state: str, reason: str = "", model: str | None = None):
        changed = (state, reason, self.model if model is None else model) != (self.state, self.reason, self.model)
        if model is not None:
            self.model = model
        if changed:
            self.state, self.reason = state, reason
            self.since = time.monotonic()
            try:
                self.on_state()
            except Exception:
                log.debug("meter on_state failed", exc_info=True)

    # ---- thread
    def run(self):
        t = None
        try:
            while not self.stop_ev.is_set():
                if self._pause.is_set():
                    if t is not None:
                        self._close(t)
                        t, self.link = None, None
                    self._paused_ack.set()
                    time.sleep(0.05)
                    continue
                if t is None:
                    t = self._connect()
                    if t is None:
                        self.stop_ev.wait(self.retry_s)
                        continue
                if not self._poll_once(t):
                    self._close(t)
                    t, self.link = None, None
                    self.stop_ev.wait(0.2)
        except Exception:                                            # a bug must not kill the thread silently or print a traceback each second
            log.exception("meter reader stopped")
            self._set("lost", "internal error in the meter reader (see the log)")
        finally:
            if t is not None:
                self._close(t)

    @staticmethod
    def _close(t):
        try:
            t.close()
        except Exception:
            pass

    def _connect(self):
        try:
            found = self.finder()
        except NotInstalled as e:
            self._set("not_installed", str(e), "")
            return None
        except TransportError as e:
            self._set("no_cable", str(e), "")
            return None
        if not found:
            self._set("lost" if self.model or self.state in ("connected", "lost") else "no_cable",
                      "no CH9329 cable on USB (1A86:E429)", self.model if self.state in ("connected", "lost") else "")
            return None
        self.cable = (found[0].get("product") or "CH9329 cable")
        t = self.factory()
        try:
            t.open()
        except TransportError as e:
            self._set("no_cable", str(e), "")
            return None
        link = MeterLink(t)
        self.link = link
        name = ""
        try:
            for _ in range(NAME_TRIES):
                name = link.get_name()
                if name:
                    break
        except TransportError as e:
            self._close(t)
            self.link = None
            self._set("lost", str(e))
            return None
        self.stats["reconnects"] += 1
        self.last_ok = time.monotonic()
        if name:
            self._set("silent", "waiting for the first reading", name)
        else:
            self._set("silent", "the cable is there but the meter does not answer: is it on, awake and its USB function enabled?", "")
        return t

    def _poll_once(self, t) -> bool:
        link = self.link
        try:
            m = link.measure()
        except TransportError as e:
            self._set("lost", f"cable lost ({e})")
            return False
        now = time.monotonic()
        if m is None:
            self.stats["timeouts"] += 1
            if now - self.last_ok > LOST_AFTER_S:
                if self.state == "connected":
                    self._set("lost", "the meter stopped answering")
                elif self.state == "silent" and not self.model:
                    # still no answer from the meter: keep asking for the name now and then
                    try:
                        n = link.get_name()
                        if n:
                            self._set("silent", "waiting for the first reading", n)
                    except TransportError as e:
                        self._set("lost", f"cable lost ({e})")
                        return False
            return True
        self.last_ok = now
        self.times.append(now)
        if not self.model:
            try:
                n = link.get_name()
                if n:
                    self.model = n
            except TransportError:
                pass
        self._set("connected", "", self.model or link.model)
        try:
            self.on_reading(R.decode(self.model, m, time.time()))
        except Exception:
            log.debug("meter on_reading failed", exc_info=True)
        return True


def read_stable(link: MeterLink, model: str, timeout_s: float, n: int, pct, cancelled=lambda: False, want_unit=None):
    """For the dmm.read action: poll until the stability rule holds or the time is up. Returns (reading, why) - reading None on failure.
    Uses the same decode, same table and same rule as the live card."""
    win = StabilityWindow(n, pct)
    end = time.monotonic() + timeout_s
    last = None
    why = "no reading"
    while time.monotonic() < end and not cancelled():
        m = link.measure()
        if m is None:
            why = "the meter did not answer"
            continue
        r = R.decode(model, m, time.time())
        last = r
        if r.why_not:
            win.reset()
            why = r.why_not
            continue
        if win.push(r.value, r.context, r.count):
            return r, ""
        why = f"not stable yet ({win.progress()[0]}/{win.progress()[1]} readings within the band)"
    return None, why
