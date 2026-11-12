"""The live meter inside the server: reads from the reader thread, decides what may be proposed, proposes it into the test in focus
through the engine's proposal path (source `meter`), and runs auto-accept with its spoken read-back and Undo.

One owner of the cable: the MeterReader. The `dmm.read` action pauses it (pause()/resume()) while it holds the device.
The plugin never computes a verdict: the engine evaluates the value against the plan's limits as for any proposal.
Readiness: HOST-ONLY (fake and simulated meters); see docs/MULTIMETER.md for per-mode hardware status.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from decimal import Decimal

from . import reading as R
from . import units
from .link import MeterReader
from .stability import N_DEFAULT, PCT_DEFAULT, StabilityWindow
from .transport import HidTransport, SimTransport

DELAY_DEFAULT = 1.0     # TBR: seconds a stable proposal waits before auto-accept
UNDO_DEFAULT = 5.0      # TBR: seconds the Undo stays available


def _env(name, default, cast=float):
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class MeterService:
    def __init__(self, get_session, sink=None, *, simulate: bool = False, transport_factory=None, finder=None, retry_s: float = 1.0):
        self.get_session = get_session
        self.sink = sink or (lambda msg: None)
        self.simulate = simulate
        self.lock = threading.RLock()
        self.cfg = {"n": int(_env("POLYBENCH_METER_N", N_DEFAULT, int)), "pct": float(_env("POLYBENCH_METER_PCT", float(PCT_DEFAULT))),
                    "delay_s": _env("POLYBENCH_METER_DELAY_S", DELAY_DEFAULT), "undo_s": _env("POLYBENCH_METER_UNDO_S", UNDO_DEFAULT)}
        self.auto = False                       # off at every start (PM default): the operator switches it on
        self.win = StabilityWindow(self.cfg["n"], self.cfg["pct"])
        self.last: R.Reading | None = None
        self.reason = ""
        self.target: dict | None = None
        self.proposal: dict | None = None       # the meter's latest proposal: {board, test_id, id, value, unit, text, ts}
        self.latch: dict | None = None          # {ctx, value, gap}: one proposal per settled reading
        self.pending_auto: dict | None = None   # {token, due, timer}
        self.undo_info: dict | None = None      # {board, test_id, event, value, unit, text, until}
        self.note = ""                          # last outcome line (accepted / undone / refused)
        self._token = 0
        self._last_push = 0.0
        self._sim: SimTransport | None = None
        if transport_factory is None:
            if simulate:
                self._sim = SimTransport(noise=True)
                transport_factory = lambda: self._sim              # noqa: E731
                finder = finder or (lambda: ([{"product": "simulated CH9329 cable"}] if self._sim.plugged else []))
            else:
                transport_factory = HidTransport
        self.reader = MeterReader(transport_factory, self.handle, self._on_conn, finder=finder, retry_s=retry_s)

    # ---- lifecycle
    def start(self):
        if not self.reader.is_alive():
            self.reader.start()

    def stop(self):
        self.reader.stop()
        with self.lock:
            self._cancel_auto()

    def pause(self) -> bool:
        """For the dmm.read action: release the cable. Proposals stop while paused."""
        with self.lock:
            self._cancel_auto()
        ok = self.reader.pause()
        self._push(force=True)
        return ok

    def resume(self):
        self.reader.resume()
        self._push(force=True)

    def sim_scene(self, name=None, **kw):
        if self._sim is None:
            raise ValueError("not a simulated meter")
        self._sim.set_scene(name, **kw)

    def set_auto(self, on: bool):
        with self.lock:
            self.auto = bool(on)
            if not self.auto:
                self._cancel_auto()
            self.note = "auto-accept on" if self.auto else "auto-accept off"
        self._push(force=True)

    def set_config(self, patch: dict) -> dict:
        new = dict(self.cfg)
        for k, cast, lo, hi in (("n", int, 2, 50), ("pct", float, 0.0, 10.0), ("delay_s", float, 0.0, 30.0), ("undo_s", float, 1.0, 120.0)):
            if k in patch:
                try:
                    v = cast(patch[k])
                except (TypeError, ValueError):
                    raise ValueError(f"{k} must be a number") from None
                if not lo <= v <= hi:
                    raise ValueError(f"{k} must be between {lo} and {hi}")
                new[k] = v
        with self.lock:
            self.cfg = new
            self.win = StabilityWindow(new["n"], new["pct"])
        self._push(force=True)
        return dict(new)

    # ---- plumbing
    def _on_conn(self):
        with self.lock:
            if self.reader.state != "connected":
                self.win.reset()
                self._cancel_auto()
                self.last = None
                self.note = ""
                if self.latch:
                    self.latch["gap"] = True
        self._push(force=True)

    def _push(self, force: bool = False):
        now = time.monotonic()
        if not force and now - self._last_push < 0.12:
            return
        self._last_push = now
        try:
            self.sink({"kind": "meter", "state": self.public_state()})
        except Exception:
            pass

    def _sess(self):
        try:
            return self.get_session()
        except Exception:
            return None

    def _focus(self) -> tuple[dict | None, str]:
        """The test in focus (the same focus the UI posts for the AI connector) as a target, or (None, why)."""
        s = self._sess()
        if s is None:
            return None, "no project is open"
        hub = getattr(s, "ai_hub", None)
        f = dict(getattr(hub, "focus", None) or {})
        b, tid = f.get("board"), f.get("test")
        if not b or not tid:
            return None, "no test in focus: open a measurement test in Polybench"
        eng = s.engine
        if tid not in eng.state.test_index:
            return None, "no test in focus: open a measurement test in Polybench"
        _, _, t = eng.state.test_index[tid]
        tgt = {"board": b, "test_id": tid, "title": t.get("title", ""), "unit": t.get("unit") or "", "kind": t.get("kind", "")}
        if t.get("kind") != "number":
            return tgt, f"{tid} takes a {t.get('kind')} result, not a measured number"
        return tgt, ""

    def _voice_busy(self) -> bool:
        s = self._sess()
        v = getattr(s, "_voice", None) if s is not None else None
        try:
            return bool(v and v.meter_blocked())
        except Exception:
            return False

    # ---- the decision
    def handle(self, r: R.Reading):
        with self.lock:
            self.last = r
            self._evaluate(r)
        self._push()

    def _evaluate(self, r: R.Reading):
        self.target, tw = self._focus()
        if r.why_not:
            self.win.reset()
            if self.latch:
                self.latch["gap"] = True
            self._cancel_auto("the reading changed")
            self.reason = r.why_not
            return
        stable = self.win.push(r.value, r.context, r.count)
        have, n = self.win.progress()
        if self.target is None or tw:
            self.reason = tw
            self._cancel_auto("no target")
            return
        conv, why = R.to_unit(r, self.target["unit"])
        if conv is None:
            self.reason = why
            self._cancel_auto("no conversion")
            return
        if not stable:
            self.reason = f"waiting for a stable reading ({have}/{n})"
            if self.pending_auto:
                self._cancel_auto("the reading moved")
            return
        mean = self.win.mean()
        if self.latch:
            same = self.latch["ctx"] == r.context and abs(mean - self.latch["value"]) <= max(abs(self.latch["value"]) * self.win.pct / 100, r.count)
            if same and not self.latch["gap"]:
                self.reason = "this reading was already proposed: move the probe (or change the reading) for the next value"
                return
            self.latch = None
        if self._voice_busy():
            self.reason = "voice is active: the meter waits"
            return
        self._propose(r, conv, mean)

    def _propose(self, r: R.Reading, conv: str, mean: Decimal):
        s = self._sess()
        eng = s.engine
        tgt = self.target
        m = r.meas
        src = {"kind": "meter", "component": r.model or "meter", "key": "meter", "operator": eng.operator, "model": r.model}
        evidence = (f"{r.model} mode {r.mode_name} (0x{m.mode:02x}) range {m.range_idx} display '{m.display}' "
                    f"flags {m.flag_bytes[0]:02x} {m.flag_bytes[1]:02x} {m.flag_bytes[2]:02x} frame {m.hex}")
        try:
            e = eng.add_proposal(tgt["board"], tgt["test_id"], conv, tgt["unit"], evidence=evidence, raw=m.hex, source=src,
                                 origin="simulated" if eng.simulated else "measured")
        except Exception as ex:                       # a refusal is a line on the card, never a traceback
            self.reason = f"the engine refused the proposal: {ex}"
            return
        self.latch = {"ctx": r.context, "value": mean, "gap": False}
        self.proposal = {"board": tgt["board"], "test_id": tgt["test_id"], "id": e["id"], "value": conv, "unit": tgt["unit"],
                         "text": r.text, "ts": time.time(), "verdict": (e.get("payload") or {}).get("verdict")}
        self.undo_info = None
        self.note = ""
        self.reason = "proposed to " + tgt["test_id"] + " on " + tgt["board"]
        self._broadcast_state()
        if self.auto:
            self._arm_auto()

    # ---- auto-accept
    def _arm_auto(self):
        self._cancel_auto()
        self._token += 1
        tok = self._token
        delay = float(self.cfg["delay_s"])
        t = threading.Timer(delay, self._auto_fire, (tok,))
        t.daemon = True
        self.pending_auto = {"token": tok, "due": time.monotonic() + delay, "timer": t}
        t.start()

    def _cancel_auto(self, why: str = ""):
        p = self.pending_auto
        self.pending_auto = None
        if p:
            p["timer"].cancel()
            if why:
                self.note = f"auto-accept cancelled: {why}"

    def _auto_fire(self, tok: int):
        with self.lock:
            p = self.pending_auto
            if not p or p["token"] != tok or not self.auto:
                return
            self.pending_auto = None
            if self._voice_busy():
                self.note = "auto-accept cancelled: voice is active"
                self.reason = "voice is active: the meter waits"
                self._push(force=True)
                return
            prop = self.proposal
            s = self._sess()
            if not prop or s is None:
                return
            eng = s.engine
            b, tid = prop["board"], prop["test_id"]
            c = eng.state.cell(b, tid)
            if not any(q["id"] == prop["id"] for q in c["proposals"].values()):
                self.note = "auto-accept cancelled: the proposal is gone (accepted or rejected by hand)"
                self._push(force=True)
                return
            try:
                res = eng.accept(b, tid, prop["id"], via="meter-auto")
            except Exception as ex:
                self.note = f"auto-accept refused: {ex}"
                self.reason = f"held for you: {ex}"
                self._push(force=True)
                return
            c = eng.state.cell(b, tid)
            ev_id = c.get("event")
            spoken = units.spoken_value(prop["value"], prop["unit"]) + ", accepted"
            self.note = f"accepted {prop['value']} {units.shown(prop['unit'])} on {b} {tid}"
            if res.get("unchanged"):
                self.note = f"{b} {tid} already held this value"
            elif res.get("stop"):
                self.note += " (a stop rule tripped: acknowledge it)"
            elif ev_id:
                self.undo_info = {"board": b, "test_id": tid, "event": ev_id, "value": prop["value"], "unit": prop["unit"],
                                  "until": time.monotonic() + float(self.cfg["undo_s"])}
            self.proposal = None
            self.reason = "accepted; focus moved to the next test"
        self._speak(spoken)
        self._broadcast_state()
        self.sink({"kind": "meter_advance", "board": b, "test_id": tid})
        self._push(force=True)

    def _speak(self, text: str):
        s = self._sess()
        try:
            s.voice.tts.say(text, kind="ack")
        except Exception:
            pass

    def undo(self) -> dict:
        with self.lock:
            u = self.undo_info
            if not u or time.monotonic() > u["until"]:
                self.undo_info = None
                raise ValueError("nothing to undo (the window has passed)")
            s = self._sess()
            s.engine.undo_accept(u["board"], u["test_id"], u["event"], via="meter-undo")
            self.undo_info = None
            if self.latch:
                self.latch["gap"] = False           # the reading is still the same: it must not be proposed again
            self.note = f"undone: {u['board']} {u['test_id']} is empty again"
            out = {"ok": True, "board": u["board"], "test_id": u["test_id"]}
        self._broadcast_state()
        self.sink({"kind": "meter_focus", "board": out["board"], "test_id": out["test_id"]})
        self._push(force=True)
        return out

    def _broadcast_state(self):
        self.sink({"kind": "state"})

    # ---- what the card shows
    def public_state(self) -> dict:
        with self.lock:
            st = self.reader.status()
            r = self.last
            now = time.monotonic()
            have, n = self.win.progress()
            u = self.undo_info
            if u and now > u["until"]:
                self.undo_info = u = None
            pa = self.pending_auto
            rd = None
            if r is not None and st["state"] == "connected":
                rd = {"label": R.label(r), "text": r.text, "kind": r.kind, "unit": units.shown(r.unit), "mode": r.mode_name, "range": r.meas.range_idx,
                      "ac": r.ac, "flags": r.meas.flags.display(), "status": r.status, "age_s": round(time.time() - r.ts, 2),
                      "hex": r.meas.hex, "why_not": r.why_not}
            tgt = self.target if st["state"] == "connected" else None
            prop = self.proposal
            if prop:                                   # drop it from the card once it is no longer pending
                s = self._sess()
                try:
                    c = s.engine.state.cell(prop["board"], prop["test_id"])
                    if not any(q["id"] == prop["id"] for q in c["proposals"].values()):
                        self.proposal = prop = None
                except Exception:
                    pass
            return {"enabled": True, "simulated": self.simulate, "conn": st, "reading": rd, "target": tgt, "reason": self.reason if rd else "",
                    "stable": {"have": have, "n": n}, "proposal": prop, "note": self.note,
                    "auto": {"on": self.auto, "delay_s": self.cfg["delay_s"], "undo_s": self.cfg["undo_s"],
                             "due_in_s": round(max(0.0, pa["due"] - now), 2) if pa else None},
                    "undo": {"board": u["board"], "test_id": u["test_id"], "value": u["value"], "unit": units.shown(u["unit"]),
                             "left_s": round(max(0.0, u["until"] - now), 1)} if u else None,
                    "cfg": dict(self.cfg), "install": "pip install -e '.[dmm]'" if st["state"] == "not_installed" else ""}


def disabled_state() -> dict:
    return {"enabled": False}
