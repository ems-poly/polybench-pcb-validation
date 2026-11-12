"""esptool.identify, esptool.flash and serial.query (examples/esp32-s3-board). HOST-ONLY: simulator, child process, fake esptool output
and a fake serial device. No board is connected."""
import importlib.util
import json
from pathlib import Path

import pytest

import polybench.plugins.esp32 as esp
import polybench.plugins.serialq as sq
from polybench import protocol
from polybench.plugins._util import ParamError, PluginError
from pb_plugin_helpers import run_child

needs_esptool = pytest.mark.skipif(importlib.util.find_spec("esptool") is None, reason="needs the esp32 extra (esptool)")
needs_serial = pytest.mark.skipif(importlib.util.find_spec("serial") is None, reason="needs the esp32 extra (pyserial)")

EX = Path(__file__).resolve().parents[2] / "examples" / "esp32-s3-board"
IMAGE = EX / "firmware" / "prebuilt" / "esp32s3-devkitc-demo.factory.bin"

IDENT = """esptool v5.6.0
Connected to ESP32-S3 on /dev/cu.usbmodem1101:
Chip type:          ESP32-S3 (QFN56) (revision v0.2)
Features:           Wi-Fi, BLE, Embedded PSRAM 8MB (AP_3v3)
MAC:                7C:DF:A1:00:42:5E
Manufacturer: c8
Detected flash size: 16MB
"""


class Ctx:
    def __init__(self, params, simulate=False, answer="Yes", tmp=None):
        self.params, self.simulate, self.board, self.workdir = params, simulate, "D01", tmp or Path(".")
        self.proposals, self.logs, self.asked = [], [], []
        self._answer = answer

    def propose(self, t, v, unit=None, evidence=None, raw=None): self.proposals.append((t, v, unit))
    def log(self, text, level="info"): self.logs.append((level, text))
    def progress(self, f, text=""): pass
    def cancelled(self): return False
    def ask(self, prompt, options): self.asked.append(prompt); return self._answer
    def artifact(self, kind, data): pass


def test_selftests():
    assert esp.EsptoolIdentify().selftest() and esp.EsptoolFlash().selftest()
    assert sq.SerialQuery().selftest()


def test_parse_identify():
    i = esp.parse_identify(IDENT)
    assert (i["chip"], i["mac"], i["flash_mb"], i["psram_mb"]) == ("ESP32-S3", "7c:df:a1:00:42:5e", 16, 8)


def test_classify():
    assert "to answer" in esp.classify("A fatal error occurred: Failed to connect to ESP32-S3: No serial data received.")
    assert "cannot be opened" in esp.classify("could not open port /dev/cu.x: resource busy")
    assert esp.classify("fine") is None


def test_identify_simulated_child(tmp_path):
    msgs, err, rc = run_child("esptool.identify", {"map": {"chip": "F03", "mac": "F04", "flash_mb": "F05", "psram_mb": "F06"}}, tmp_path)
    assert rc == 0, err
    props = {m["test_id"]: m["value"] for m in msgs if m["t"] == "proposal"}
    assert props["F03"] == "ESP32-S3" and props["F05"] == "16" and props["F06"] == "8" and props["F04"].startswith("7c:df:a1:")
    assert msgs[-1]["t"] == "done"
    assert not any(m["t"] == "verdict" for m in msgs)


def test_flash_simulated_child_hashes_real_file(tmp_path):
    msgs, err, rc = run_child("esptool.flash", {"image": str(IMAGE), "map": {"flashed": "F01", "image_sha256": "F02"}}, tmp_path)
    assert rc == 0, err
    props = {m["test_id"]: m["value"] for m in msgs if m["t"] == "proposal"}
    assert props["F01"] == "Y" and len(props["F02"]) == 64
    assert props["F02"] == (EX / "firmware/prebuilt/MANIFEST.txt").read_text(encoding="utf-8").split()[0]


def test_flash_refuses_missing_image_and_elf(tmp_path):
    msgs, err, rc = run_child("esptool.flash", {"image": str(tmp_path / "none.bin")}, tmp_path)
    assert rc == 1 and msgs[-1]["t"] == "error" and "image not found" in msgs[-1]["text"]
    elf = tmp_path / "a.elf"; elf.write_bytes(b"x")
    msgs, err, rc = run_child("esptool.flash", {"image": str(elf)}, tmp_path)
    assert rc == 1 and "raw .bin" in msgs[-1]["text"]


def test_safe_state_child():
    for name in ("esptool.identify", "esptool.flash", "serial.query"):
        msgs, err, rc = run_child(name, {}, ".", safe_state=True)
        assert rc == 0 and msgs[-1]["t"] == "done" and msgs[-1]["text"]


def test_identify_real_path_with_fake_esptool(monkeypatch, tmp_path):
    monkeypatch.setattr(esp, "pick_port", lambda p: "/dev/cu.fake")
    monkeypatch.setattr(esp, "run_esptool", lambda args, timeout, ctx=None: IDENT)
    c = Ctx({"map": {"chip": "F03", "mac": "F04", "flash_mb": "F05", "psram_mb": "F06"}}, tmp=tmp_path)
    esp.EsptoolIdentify().run(c)
    assert dict((t, v) for t, v, _ in c.proposals) == {"F03": "ESP32-S3", "F04": "7c:df:a1:00:42:5e", "F05": "16", "F06": "8"}


def test_flash_without_verified_hash_proposes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(esp, "pick_port", lambda p: "/dev/cu.fake")
    monkeypatch.setattr(esp, "run_esptool", lambda args, timeout, ctx=None: "Writing at 0x0...\nLeaving...\n")
    c = Ctx({"image": str(IMAGE), "map": {"flashed": "F01"}}, tmp=tmp_path)
    with pytest.raises(PluginError, match="Hash of data verified"):
        esp.EsptoolFlash().run(c)
    assert c.proposals == []
    monkeypatch.setattr(esp, "run_esptool", lambda args, timeout, ctx=None: "Hash of data verified.\n")
    esp.EsptoolFlash().run(c)
    assert c.proposals == [("F01", "Y", None)]


def test_pick_port_rules(monkeypatch):
    monkeypatch.setattr(esp, "candidate_ports", lambda: [])
    with pytest.raises(PluginError, match="No ESP32 board"):
        esp.pick_port({})
    monkeypatch.setattr(esp, "candidate_ports", lambda: [{"device": "/dev/cu.a", "desc": "x"}, {"device": "/dev/cu.b", "desc": "y"}])
    with pytest.raises(PluginError, match="More than one"):
        esp.pick_port({})
    assert esp.pick_port({"port": "/dev/cu.z"}) == "/dev/cu.z"


@needs_esptool
def test_run_esptool_times_out_and_stops_child(monkeypatch):
    import sys, subprocess
    real = subprocess.Popen
    monkeypatch.setattr(esp.subprocess, "Popen", lambda a, **k: real([sys.executable, "-c", "import time; time.sleep(30)"], **k))
    with pytest.raises(PluginError, match="did not finish"):
        esp.run_esptool(["x"], 1.0)


class FakeSerial:
    """A device that follows the demo firmware's protocol: `<cmd> #<id>` in, one JSON line with the same id out.
    `delay` maps a command name to the seconds before its reply becomes readable (a slow command)."""

    def __init__(self, replies, delay=None, boot_line=True):
        import time
        self.replies, self.delay, self.closed, self.sent = replies, delay or {}, False, []
        self.queue = [(0, (json.dumps({"type": "selftest", "flash_mb": 1}) + "\n").encode())] if boot_line else []
        self._time = time

    def reset_input_buffer(self): self.queue = [q for q in self.queue if q[0] > self._time.monotonic()]

    def write(self, b):
        parts = b.decode().split()
        self.sent.append(b.decode().strip())
        cmd, rid = parts[0], int(parts[-1][1:]) if parts[-1].startswith("#") else None
        if cmd in self.replies:
            obj = dict(self.replies[cmd] if not callable(self.replies[cmd]) else self.replies[cmd]())
            if rid is not None:
                obj["id"] = rid
            self.queue.append((self._time.monotonic() + self.delay.get(cmd, 0), (json.dumps(obj) + "\n").encode()))

    def flush(self): pass

    def read(self, n):
        now = self._time.monotonic()
        ready = [q for q in self.queue if q[0] <= now]
        if not ready:
            self._time.sleep(0.02)
            return b""
        self.queue.remove(ready[0])
        return ready[0][1]

    def close(self): self.closed = True


def _use(monkeypatch, fake):
    import serial
    monkeypatch.setattr(serial, "Serial", lambda *a, **k: fake)
    monkeypatch.setattr(sq, "pick_port", lambda p: "/dev/cu.fake")
    monkeypatch.setattr(sq, "candidate_ports", lambda: [])


@needs_serial
def test_serial_query_real_path_with_fake_port(monkeypatch):
    fake = FakeSerial({"selftest": {"type": "selftest", "flash_mb": 16, "psram_mb": 8, "mac": "aa"}, "led": {"type": "led", "gpio": 38},
                       "wifi": {"type": "wifi", "count": 5}})
    _use(monkeypatch, fake)
    steps = [{"send": "selftest", "timeout_s": 2, "map": {"flash_mb": "F07", "psram_mb": "F08"}},
             {"send": "led", "timeout_s": 2, "ask": {"prompt": "Blink?", "test": "F09"}},
             {"send": "wifi", "timeout_s": 2, "map": {"count": "F10"}}]
    c = Ctx({"steps": steps, "settle_s": 0}, answer="No")
    sq.SerialQuery().run(c)
    assert dict((t, v) for t, v, _ in c.proposals) == {"F07": "16", "F08": "8", "F09": "N", "F10": "5"}
    assert c.asked == ["Blink?"] and fake.closed
    assert fake.sent == ["selftest #1", "led #2", "wifi #3"]   # one send per step, a new id each time


@needs_serial
def test_serial_query_boot_line_without_id_is_not_a_reply(monkeypatch):
    fake = FakeSerial({"selftest": {"type": "selftest", "flash_mb": 16}})   # the boot-time line says flash_mb 1, no id
    _use(monkeypatch, fake)
    c = Ctx({"steps": [{"send": "selftest", "timeout_s": 2, "map": {"flash_mb": "F07"}}], "settle_s": 0})
    sq.SerialQuery().run(c)
    assert c.proposals == [("F07", "16", None)]


@needs_serial
def test_serial_query_late_reply_is_never_taken_for_the_next_command(monkeypatch):
    """The first selftest answers after its attempt timed out (0.5 s > 0.3 s). Its late line carries id 1 and must be
    dropped while the second step waits for id 2. Before ids, the late line (flash_mb 99) was taken for step two."""
    answers = iter([{"type": "selftest", "flash_mb": 99}, {"type": "selftest", "flash_mb": 16}])
    fake = FakeSerial({"selftest": lambda: next(answers)}, boot_line=False)
    fake.delay["selftest"] = 0.5
    _use(monkeypatch, fake)
    c = Ctx({"steps": [{"send": "selftest", "timeout_s": 0.3, "map": {"flash_mb": "F07"}},
                       {"send": "selftest", "timeout_s": 2.0, "map": {"flash_mb": "F08"}}], "settle_s": 0})
    with pytest.raises(PluginError, match="No 'selftest' reply"):
        sq.SerialQuery().run(c)            # step 1 failed, the run reports it at the end
    assert c.proposals == [("F08", "16", None)], c.proposals   # step 2 got its own reply (id 2), not the late id 1


@needs_serial
def test_serial_query_slow_command_needs_no_resend_with_a_long_enough_timeout(monkeypatch):
    fake = FakeSerial({"led": {"type": "led", "gpio": 38}}, delay={"led": 0.6}, boot_line=False)
    _use(monkeypatch, fake)
    c = Ctx({"steps": [{"send": "led", "timeout_s": 2, "retries": 1, "map": {"gpio": "F20"}}], "settle_s": 0})
    sq.SerialQuery().run(c)
    assert fake.sent == ["led #1"] and c.proposals == [("F20", "38", None)]   # sent once: the LED does not blink twice


@needs_serial
def test_serial_query_late_reply_to_an_earlier_attempt_of_the_same_step_is_accepted(monkeypatch):
    fake = FakeSerial({"wifi": {"type": "wifi", "count": 4}}, delay={"wifi": 0.5}, boot_line=False)
    _use(monkeypatch, fake)
    c = Ctx({"steps": [{"send": "wifi", "timeout_s": 0.3, "retries": 2, "map": {"count": "F10"}}], "settle_s": 0})
    sq.SerialQuery().run(c)
    assert fake.sent[0] == "wifi #1" and c.proposals[0] == ("F10", "4", None)


@needs_serial
def test_serial_query_requires_timeout_per_step(monkeypatch):
    _use(monkeypatch, FakeSerial({}))
    c = Ctx({"steps": [{"send": "selftest"}], "settle_s": 0})
    with pytest.raises(PluginError, match="timeout_s is required"):
        sq.SerialQuery().run(c)


@needs_serial
def test_serial_query_no_reply_raises_and_closes(monkeypatch):
    fake = FakeSerial({}, boot_line=False)
    _use(monkeypatch, fake)
    with pytest.raises(PluginError, match="not in download mode"):
        sq.SerialQuery().run(Ctx({"steps": [{"send": "selftest", "timeout_s": 0.6, "retries": 0}], "settle_s": 0}))
    assert fake.closed


@needs_serial
def test_serial_query_cancel_returns_and_closes(monkeypatch):
    fake = FakeSerial({}, boot_line=False)
    _use(monkeypatch, fake)
    c = Ctx({"steps": [{"send": "selftest", "timeout_s": 30}], "settle_s": 0})
    c.cancelled = lambda: True
    sq.SerialQuery().run(c)      # returns normally: run_action then ends with {"t":"done","cancelled":true}
    assert fake.closed


def test_run_esptool_cancel_returns_none(monkeypatch):
    import sys, subprocess
    real = subprocess.Popen
    monkeypatch.setattr(esp.subprocess, "Popen", lambda a, **k: real([sys.executable, "-c", "import time; time.sleep(30)"], **k))
    monkeypatch.setattr(esp, "esptool_available", lambda: True)
    c = Ctx({})
    c.cancelled = lambda: True
    assert esp.run_esptool(["x"], 20, c) is None


@needs_serial
def test_no_board_is_one_clear_line_and_no_traceback(tmp_path):
    """The child as the core starts it, with the port list forced empty: the error is one line, stderr (the operator
    console) stays empty, and the traceback is in the debug log in the run folder."""
    import subprocess, sys
    for name, params in (("esptool.identify", {}), ("serial.query", {"steps": [{"send": "selftest", "timeout_s": 1}]})):
        code = ("import sys, json; import polybench.plugins.esp32 as e; e.candidate_ports = lambda: []; "
                "from polybench.run_action import main; "
                f"sys.exit(main([{name!r}, json.dumps({{'board': 'S01', 'params': {params!r}, 'workdir': {str(tmp_path)!r}}})]))")
        env = {k: v for k, v in __import__("os").environ.items() if k != "POLYBENCH_ESP_PORT"}
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
        p = subprocess.run([sys.executable, "-c", code], stdin=subprocess.PIPE, capture_output=True, text=True, env=env, timeout=30, encoding="utf-8", errors="replace")
        lines = [json.loads(x) for x in p.stdout.splitlines() if x.strip()]
        assert p.returncode == 1 and lines[-1]["t"] == "error"
        assert "No ESP32 board found" in lines[-1]["text"] and "\n" not in lines[-1]["text"]
        assert "Micro-USB" in lines[-1]["text"] and "USB-C" not in lines[-1]["text"]
        assert p.stderr.strip() == "", p.stderr
        log = tmp_path / "action_debug.log"
        assert "Traceback" in log.read_text(encoding="utf-8")
        log.unlink()


def test_plugin_cancel_ends_with_done_cancelled(tmp_path):
    """serial.query in the simulator, cancel sent while it runs: the child exits 0 with done/cancelled, not error."""
    import subprocess, sys, time
    args = {"board": "S01", "simulate": True, "workdir": str(tmp_path),
            "params": {"steps": [{"send": "selftest", "timeout_s": 5}] * 40}}
    env = {**__import__("os").environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    p = subprocess.Popen([sys.executable, "-m", "polybench.run_action", "serial.query", json.dumps(args)], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, encoding="utf-8", errors="replace")
    time.sleep(1.0)
    p.stdin.write(json.dumps({"t": "cancel"}) + "\n"); p.stdin.flush()
    out, err = p.communicate(timeout=20)
    msgs = [json.loads(x) for x in out.splitlines() if x.strip()]
    assert p.returncode == 0 and msgs[-1] == {"t": "done", "cancelled": True}, (msgs[-1], err)


@needs_serial
def test_serial_query_simulated_child_asks_led(tmp_path):
    steps = [{"send": "selftest", "timeout_s": 6, "map": {"flash_mb": "F07", "psram_mb": "F08", "mac": "F11"}}, {"send": "wifi", "timeout_s": 15, "map": {"count": "F10"}}]
    msgs, err, rc = run_child("serial.query", {"steps": steps}, tmp_path)
    assert rc == 0, err
    props = {m["test_id"]: m["value"] for m in msgs if m["t"] == "proposal"}
    assert props["F07"] == "16" and props["F08"] == "8" and props["F10"] == "7"


def test_example_protocol_loads_clean():
    p = protocol.load(str(EX / "devkitc-1.pcbv.yaml"))
    bad = [i for i in p.issues if i.level == "error"]
    assert not bad, bad
    ids = {t["id"] for s in p.data["steps"] for sec in s.get("sections", []) for t in sec.get("tests", [])}
    assert {"U01", "P01", "F01", "F09", "F10", "V01"} <= ids
    for s in p.data["steps"]:
        for sec in s.get("sections", []):
            for a in sec.get("actions", []):
                assert a["plugin"] in ("esptool.flash", "esptool.identify", "serial.query")


# ---- fix round: N2 cancel in the simulated paths, S4 parameter checks, D4 sha256 param

class CancelCtx(Ctx):
    def cancelled(self): return True


@pytest.mark.parametrize("action,params", [
    (esp.EsptoolFlash, {"image": str(IMAGE), "map": {"flashed": "F01", "image_sha256": "F02"}}),
    (esp.EsptoolIdentify, {"map": {"chip": "F03", "mac": "F04", "flash_mb": "F05", "psram_mb": "F06"}}),
])
def test_simulated_cancel_proposes_nothing(action, params):
    c = CancelCtx(params, simulate=True)
    action().run(c)          # returns normally: the child then ends with done/cancelled
    assert c.proposals == []


def test_simulated_cancel_midway_proposes_nothing():
    class Late(Ctx):
        n = 0
        def cancelled(self):
            self.n += 1
            return self.n > 3
    c = Late({"image": str(IMAGE), "map": {"flashed": "F01", "image_sha256": "F02"}}, simulate=True)
    esp.EsptoolFlash().run(c)
    assert c.proposals == []


@pytest.mark.parametrize("step,msg", [
    ({"send": "selftest", "timeout_s": 0}, "greater than 0"),
    ({"send": "selftest", "timeout_s": -2}, "greater than 0"),
    ({"send": "selftest", "timeout_s": "soon"}, "not a number"),
    ({"send": "selftest", "timeout_s": True}, "not a number"),
    ({"send": "selftest", "timeout_s": 2, "retries": -1}, "0 or more"),
    ({"send": "selftest", "timeout_s": 2, "retries": "x"}, "not an integer"),
])
@pytest.mark.parametrize("simulate", [True, False])
@needs_serial
def test_serial_query_rejects_bad_params(monkeypatch, step, msg, simulate):
    _use(monkeypatch, FakeSerial({}))
    c = Ctx({"steps": [step], "settle_s": 0}, simulate=simulate)
    with pytest.raises(ParamError, match=msg) as ei:
        sq.SerialQuery().run(c)
    assert "\n" not in str(ei.value) and "Traceback" not in str(ei.value)


@needs_serial
def test_serial_query_simulated_timeout_enforced():
    c = Ctx({"steps": [{"send": "selftest", "timeout_s": 0.01}]}, simulate=True)
    with pytest.raises(PluginError, match="No 'selftest' reply"):
        sq.SerialQuery().run(c)
    c = Ctx({"steps": [{"send": "selftest", "timeout_s": 2}]}, simulate=True)
    sq.SerialQuery().run(c)


@needs_serial
def test_serial_query_simulated_requires_timeout():
    with pytest.raises(PluginError, match="timeout_s is required"):
        sq.SerialQuery().run(Ctx({"steps": [{"send": "selftest"}]}, simulate=True))


def test_esptool_rejects_bad_timeout():
    for bad in (0, -5, "x"):
        with pytest.raises(ParamError):
            esp.EsptoolIdentify().run(Ctx({"timeout_s": bad}, simulate=True))
        with pytest.raises(ParamError):
            esp.EsptoolFlash().run(Ctx({"image": str(IMAGE), "timeout_s": bad}, simulate=True))


def test_flash_sha256_param_matches_or_refuses():
    good = (EX / "firmware/prebuilt/MANIFEST.txt").read_text(encoding="utf-8").split()[0]
    c = Ctx({"image": str(IMAGE), "sha256": good, "map": {"flashed": "F01"}}, simulate=True)
    esp.EsptoolFlash().run(c)
    assert c.proposals
    with pytest.raises(ParamError, match="does not match"):
        esp.EsptoolFlash().run(Ctx({"image": str(IMAGE), "sha256": "0" * 64}, simulate=True))


def test_protocol_flash_action_carries_manifest_sha256():
    txt = (EX / "function.yaml").read_text(encoding="utf-8")
    assert (EX / "firmware/prebuilt/MANIFEST.txt").read_text(encoding="utf-8").split()[0] in txt


def test_flash_simulated_without_the_binary_uses_the_pinned_hash(tmp_path):
    """Wheel copies of the example carry no firmware binary; the simulated flash must still propose F01 and F02."""
    pin = "ab" * 32
    params = {"image": str(tmp_path / "absent.factory.bin"), "sha256": pin, "simulate_without_image": True, "map": {"flashed": "F01", "image_sha256": "F02"}}
    msgs, err, rc = run_child("esptool.flash", params, tmp_path)
    assert rc == 0, err
    props = {m["test_id"]: m["value"] for m in msgs if m["t"] == "proposal"}
    assert props == {"F01": "Y", "F02": pin}
    # the escape hatch needs both the flag and a pinned hash; without them a missing image is still refused
    for drop in ("simulate_without_image", "sha256"):
        q = {k: v for k, v in params.items() if k != drop}
        msgs, err, rc = run_child("esptool.flash", q, tmp_path)
        assert rc == 1 and "image not found" in msgs[-1]["text"]


def test_example_flash_actions_declare_simulate_without_image():
    import yaml
    for f in ("function.yaml", "function_devkitc-1.yaml"):
        d = yaml.safe_load((EX / f).read_text(encoding="utf-8"))
        flash = [a for s in d["steps"] for g in s.get("sections", []) for a in g.get("actions", []) if a["plugin"] == "esptool.flash"]
        assert flash and all(a["params"].get("simulate_without_image") and a["params"].get("sha256") for a in flash), f
