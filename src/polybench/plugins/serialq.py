"""Action `serial.query`: send line commands to a device over a serial port and read one JSON line back per command.

Generic on purpose: the protocol lists `steps`, each with a command to send, the JSON `type` to wait for, and a `map` from
JSON key to test ID. A step may also `ask` the operator a yes/no question after the command (an LED blink, a buzzer) and
propose the answer. Needs pyserial (comes with the `esp32` extra). Opening the port can reset a board on some USB bridges.

The firmware must be RUNNING (not in the ROM download mode that BOOT + RESET enters), and the port must be the one the
firmware's `Serial` is wired to (on a DevKitC-1 built with ARDUINO_USB_CDC_ON_BOOT=0 that is the UART / USB-bridge port).

Replies are matched to requests: each attempt sends `<command> #<id>` and only a JSON line that carries the same `id`
(and the expected `type`) is accepted, so a late reply to an earlier command is dropped instead of being taken for the
next one. `timeout_s` is REQUIRED on every step and is the time allowed for ONE attempt: set it longer than the slowest
the command can take (an LED blink of 4.8 s needs more than 5). A step is re-sent only after a whole attempt timed out,
and only `retries` times (default 0); a reply to an earlier attempt of the same step is still accepted.

    params:
      port, baud (115200), settle_s (1.5, after opening)
      steps: [{send: "selftest", expect: "selftest", timeout_s: 8, retries: 1,
               map: {flash_mb: F03}, units: {flash_mb: MB}, yn: [key,...],
               ask: {prompt: "...", test: F05}}]

Proposals only; no verdicts. The port is opened and closed inside the action, so there is nothing to release (`safe_state`).
Readiness: HOST-ONLY (simulator, fake serial device in tests).
"""
from __future__ import annotations

import hashlib
import json
import time

from ._util import ParamError, PluginError, need, retries_param, timeout_param, to_int
from .esp32 import candidate_ports, pick_port

SIM_REPLY = {"selftest": {"type": "selftest", "fw": "polybench-esp32-demo 1.0.0", "chip": "ESP32-S3", "revision": 2, "cores": 2,
                          "flash_mb": 16, "psram_mb": 8, "free_heap_kb": 291},
             "wifi": {"type": "wifi", "count": 7}, "led": {"type": "led", "gpio": 38}}


def _sim_reply(ctx, send: str) -> dict:
    cmd = send.split()[0] if send else ""
    if cmd not in SIM_REPLY:
        raise PluginError(f"simulated device does not know the command '{cmd}'")
    r = dict(SIM_REPLY[cmd])
    if cmd == "selftest":
        h = hashlib.sha256(ctx.board.encode()).digest()
        r["mac"] = "7c:df:a1:%02x:%02x:%02x" % (h[0], h[1], h[2])
    return r


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "Y" if v else "N"
    return str(v)


SIM_REPLY_S = 0.15  # the simulated device's answer time; timeout_s is enforced against it


class SerialQuery:
    name = "serial.query"
    capabilities = ["serial"]

    def run(self, ctx) -> None:
        p = ctx.params
        steps = need(p, "steps", list)
        if not steps:
            raise ParamError("steps is empty")
        for st in steps:   # bad params are refused before any port is opened
            self._check_step(st)
        ser = None
        if not ctx.simulate:
            try:
                import serial
            except ImportError:
                raise PluginError("pyserial is not installed. Install the optional extra: pip install -e '.[esp32]'") from None
            port = pick_port(p)
            self._warn_native(ctx, port)
            try:
                ser = serial.Serial(port, to_int(p.get("baud", 115200), "baud"), timeout=0.2)
            except Exception as e:
                raise PluginError(f"cannot open {port}: {e}. Close any serial monitor using it.") from None
            ctx.log(f"opened {port}")
            time.sleep(float(p.get("settle_s", 1.5)))
        else:
            ctx.log("simulated serial device")
        failed = []
        self._next_id = 1
        try:
            for i, st in enumerate(steps):
                if ctx.cancelled():
                    break
                ctx.progress(i / len(steps), f"{st.get('send')}")
                try:
                    self._step(ctx, ser, st)
                except PluginError as e:
                    ctx.log(str(e), "error")
                    failed.append(str(e))
        finally:
            if ser is not None:
                ser.close()
        if ctx.cancelled():
            ctx.log("cancelled; the port is closed", "warn")
            return  # the child ends with {"t":"done","cancelled":true}
        ctx.progress(1.0, "done")
        if failed:
            raise PluginError("; ".join(failed))

    @staticmethod
    def _check_step(st: dict) -> tuple[float, int]:
        send = str(need(st, "send"))
        if st.get("timeout_s") in (None, ""):
            raise ParamError(f"step '{send}': timeout_s is required (seconds allowed for one attempt, longer than the slowest reply)")
        try:
            return timeout_param(st["timeout_s"], "timeout_s"), retries_param(st.get("retries", 0))
        except ParamError as e:
            raise ParamError(f"step '{send}': {e}") from None

    def _step(self, ctx, ser, st: dict) -> None:
        send = str(need(st, "send"))
        expect = st.get("expect") or send.split()[0]
        timeout, retries = self._check_step(st)
        if ctx.simulate:
            if timeout < SIM_REPLY_S:
                raise PluginError(f"No '{expect}' reply to '{send}' within {timeout:g} s per attempt (simulated device needs {SIM_REPLY_S:g} s)")
            time.sleep(SIM_REPLY_S)
            if ctx.cancelled():
                return
            reply = _sim_reply(ctx, send)
        else:
            reply = self._exchange(ctx, ser, send, expect, timeout, retries, self)
            if reply is None:  # cancelled
                return
        ctx.log(f"{send} -> {json.dumps(reply)}")
        mp = st.get("map") or {}
        yn = set(st.get("yn") or [])
        units = st.get("units") or {}
        for key, test in mp.items():
            if key not in reply:
                ctx.log(f"'{key}' not in the reply to '{send}'; not proposed", "warn")
                continue
            v = reply[key]
            ctx.propose(test, "Y" if key in yn and v else "N" if key in yn else _fmt(v), units.get(key), None, json.dumps({key: v}))
        ask = st.get("ask")
        if ask:
            ans = ctx.ask(str(need(ask, "prompt")), ["Yes", "No"])
            ctx.propose(need(ask, "test"), "Y" if str(ans).lower().startswith("y") else "N", None, None, f"operator: {ans}")

    @staticmethod
    def _warn_native(ctx, port: str) -> None:
        for c in candidate_ports():
            if c["device"] == port and "native" in c["desc"].lower():
                ctx.log(f"{port} looks like the native USB port. The demo firmware answers on UART0, which is the "
                        "UART / USB-bridge port: if there is no reply, move the cable to the other connector.", "warn")

    @staticmethod
    def _exchange(ctx, ser, send: str, expect: str, timeout: float, retries: int, owner=None) -> dict | None:
        """Send `<send> #<id>`, accept only a reply with that id and type. `timeout` is per attempt. None when cancelled."""
        ser.reset_input_buffer()  # once per step: what is already queued belongs to an earlier command
        ids: set[int] = set()
        buf = b""
        for attempt in range(retries + 1):
            nid = getattr(owner, "_next_id", 1)
            if owner is not None:
                owner._next_id = nid + 1
            ids.add(nid)
            ser.write(f"{send} #{nid}\n".encode())
            ser.flush()
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if ctx.cancelled():
                    return None
                buf += ser.read(256)
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"{"):
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if obj.get("id") not in ids:
                        continue  # boot-time line, or the late reply to an earlier command
                    if obj.get("type") == "error":
                        raise PluginError(f"The device rejected '{send}': {obj.get('text', 'error')}")
                    if obj.get("type") == expect:
                        return obj
        raise PluginError(f"No '{expect}' reply to '{send}' within {timeout:g} s per attempt ({retries + 1} attempt(s)). "
                          "The demo firmware must be running, not in download mode (after holding BOOT, press RESET alone). "
                          "Use the board's UART (USB-bridge) port: the firmware's Serial is on UART0. Check that the firmware "
                          "is the current one (run the flash action first; an old image answers without request ids) and "
                          "that no serial monitor holds the port.")

    def safe_state(self) -> str:
        return "serial port is opened and closed inside the action; nothing is held and no output is driven"

    def selftest(self) -> bool:
        return _sim_reply(type("C", (), {"board": "S01"})(), "selftest")["flash_mb"] == 16 and _fmt(True) == "Y"
