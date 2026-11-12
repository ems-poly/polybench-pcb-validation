import stat
import sys
import textwrap

import pytest

import polybench.plugins.openocd as ocd
from polybench.plugins._util import ParamError, PluginError
from polybench.plugins.openocd import (OpenocdFlash, OpenocdIdentify, ProbeError, classify_openocd_output, decode_did,
                                       parse_identify, parse_ioreg)

IDENT_OK = textwrap.dedent('''\
    Info : CMSIS-DAP: SWD supported
    Info : [samc21.cpu] Cortex-M0+ r0p1 processor detected
    SAMC21J18A rev B
    #0 : samc21.flash (at91samd) at 0x00000000, size 0x00040000, buswidth 0, chipwidth 0
    0x41002018: 00000000
    0x41002118: 11001106
    ''')


def fake_openocd(tmp_path, stdout, rc=0):
    p = tmp_path / "openocd"
    p.write_text(f"#!/bin/sh\ncat <<'EOF'\n{stdout}\nEOF\nexit {rc}\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


@pytest.fixture
def one_probe(monkeypatch):
    monkeypatch.setattr(ocd, "usb_probes", lambda *a: [{"serial": "J123", "name": "Atmel-ICE CMSIS-DAP", "vid": 0x3EB, "pid": 0x2141}])


def test_classify_messages():
    assert "No programmer" in classify_openocd_output("Error: unable to find a matching CMSIS-DAP device")
    assert "not responding" in classify_openocd_output("Error: Error connecting DP: cannot read IDR")
    assert "locked" in classify_openocd_output("Error: Device is locked, security bit set")
    assert classify_openocd_output("Info : all fine") is None


def test_parse_identify_and_did():
    i = parse_identify(IDENT_OK)
    assert i["did"] == 0x11001106 and i["flash_bytes"] == 0x40000 and i["part"] == "SAMC21J18A"
    assert decode_did(0x11001106)["processor"] == 1
    assert parse_identify("0x41002018: ffffffff\n0x41002118: 00000000")["did"] is None


def test_parse_ioreg():
    out = '''+-o Atmel-ICE CMSIS-DAP@14100000  <class IOUSBHostDevice>
      "idVendor" = 1003
      "idProduct" = 8513
      "USB Serial Number" = "J41800012345"
      "USB Product Name" = "Atmel-ICE CMSIS-DAP"
    +-o Keyboard@1
      "idVendor" = 1452
    '''
    pr = parse_ioreg(out)
    assert len(pr) == 1 and pr[0]["serial"] == "J41800012345"


def test_identify_sim(make_ctx):
    ctx = make_ctx({"map": {"probe_serial": "F1", "device_id": "F2", "flash_kb": "F3"}})
    OpenocdIdentify().run(ctx)
    assert {p["test_id"]: p["value"] for p in ctx.proposals} == {"F1": "J41800012345", "F2": "0x11001106", "F3": "256"}


def test_identify_real_ok(make_ctx, tmp_path, one_probe):
    ctx = make_ctx({"openocd": fake_openocd(tmp_path, IDENT_OK), "map": {"probe_serial": "F1", "device_id": "F2", "flash_kb": "F3"}},
                   simulate=False)
    OpenocdIdentify().run(ctx)
    assert {p["test_id"]: p["value"] for p in ctx.proposals} == {"F1": "J123", "F2": "0x11001106", "F3": "256"}


def test_identify_no_probe(make_ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(ocd, "usb_probes", lambda *a: [])
    ctx = make_ctx({"openocd": fake_openocd(tmp_path, "Error: unable to find a matching CMSIS-DAP device", 1)}, simulate=False)
    with pytest.raises(ProbeError, match="No programmer found"):
        OpenocdIdentify().run(ctx)
    assert ctx.proposals == []


def test_identify_target_silent(make_ctx, tmp_path, one_probe):
    ctx = make_ctx({"openocd": fake_openocd(tmp_path, "Error: Error connecting DP: cannot read IDR", 1)}, simulate=False)
    with pytest.raises(ProbeError, match="Target not responding"):
        OpenocdIdentify().run(ctx)


def test_identify_two_probes_needs_serial(make_ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(ocd, "usb_probes", lambda *a: [{"serial": "A", "name": "x"}, {"serial": "B", "name": "x"}])
    with pytest.raises(ProbeError, match="More than one"):
        OpenocdIdentify().run(make_ctx({"openocd": fake_openocd(tmp_path, "")}, simulate=False))


def test_identify_openocd_missing(make_ctx, monkeypatch, one_probe):
    monkeypatch.setattr(ocd.shutil, "which", lambda n: None)
    monkeypatch.setattr(ocd, "OPENOCD_CANDIDATES", ())
    with pytest.raises(PluginError, match="not installed"):
        OpenocdIdentify().run(make_ctx({}, simulate=False))


def test_identify_timeout_is_ours(make_ctx, tmp_path, one_probe, monkeypatch):
    p = tmp_path / "openocd"
    p.write_text("#!/bin/sh\nsleep 30\n")
    p.chmod(0o755)
    monkeypatch.setattr(ocd, "IDENTIFY_TIMEOUT_S", 1.0)
    with pytest.raises(PluginError, match="timed out"):
        OpenocdIdentify().run(make_ctx({"openocd": str(p)}, simulate=False))


def test_flash_sim(make_ctx, tmp_path):
    img = tmp_path / "a.hex"
    img.write_text(":00000001FF\n")
    ctx = make_ctx({"image": str(img), "map": {"flashed": "F4", "image_sha256": "F5"}})
    OpenocdFlash().run(ctx)
    got = {p["test_id"]: p["value"] for p in ctx.proposals}
    import hashlib
    assert got["F4"] == "Y" and got["F5"] == hashlib.sha256(img.read_bytes()).hexdigest()


def test_flash_param_errors(make_ctx, tmp_path):
    with pytest.raises(ParamError, match="missing"):
        OpenocdFlash().run(make_ctx({}))
    with pytest.raises(ParamError, match="not found"):
        OpenocdFlash().run(make_ctx({"image": str(tmp_path / "nope.elf")}))
    b = tmp_path / "x.bin"
    b.write_bytes(b"\0")
    with pytest.raises(ParamError, match="load address"):
        OpenocdFlash().run(make_ctx({"image": str(b)}))


def test_flash_real_verified(make_ctx, tmp_path, one_probe):
    img = tmp_path / "a.elf"
    img.write_bytes(b"\x7fELF")
    out = "** Programming Started **\n** Programming Finished **\n** Verify Started **\n** Verified OK **\n** Resetting Target **"
    ctx = make_ctx({"image": str(img), "openocd": fake_openocd(tmp_path, out), "map": {"flashed": "F4"}}, simulate=False)
    OpenocdFlash().run(ctx)
    assert ctx.proposals[0]["value"] == "Y"


def test_flash_verify_failure_proposes_nothing(make_ctx, tmp_path, one_probe):
    img = tmp_path / "a.elf"
    img.write_bytes(b"\x7fELF")
    out = "** Programming Started **\nError: checksum mismatch - attempting binary compare\n** Verify Failed **"
    ctx = make_ctx({"image": str(img), "openocd": fake_openocd(tmp_path, out, 1), "map": {"flashed": "F4"}}, simulate=False)
    with pytest.raises(ProbeError, match="did not verify"):
        OpenocdFlash().run(ctx)
    assert ctx.proposals == []


def test_flash_no_target_power(make_ctx, tmp_path, one_probe):
    img = tmp_path / "a.hex"
    img.write_text(":00000001FF\n")
    ctx = make_ctx({"image": str(img), "openocd": fake_openocd(tmp_path, "Error: Error connecting DP: cannot read IDR", 1)}, simulate=False)
    with pytest.raises(ProbeError, match="target power"):
        OpenocdFlash().run(ctx)


def test_selftest_and_safe_state():
    for c in (OpenocdIdentify, OpenocdFlash):
        assert c().selftest() is True and "released" in c().safe_state()


# ---- 15 Oct: Atmel-ICE-only filter, NVM user row and serial number

IDENT_FULL = IDENT_OK + textwrap.dedent('''\
    0x00804000: 015088ff
    0x0080a00c: 5a31c0de
    0x0080a040: 0b7a1c21
    0x0080a044: 93d04e7f
    0x0080a048: 2f8160a5
    ''')
EXTRA_MAP = {"bootprot": "F4", "bodvdd_level": "F5", "uid": "F6", "bodvdd_enabled": "F7", "nvm_user_row": "F8"}


def test_args_restrict_to_atmel_ice():
    a = OpenocdIdentify()._args({"openocd": sys.executable}, ["init"])
    i = a.index("cmsis_dap_vid_pid 0x03eb 0x2141")
    assert a[i - 1] == "-c"
    assert a.index("interface/cmsis-dap.cfg") < i < a.index("target/at91samdXX.cfg")   # after the driver, before init


def test_args_vid_pid_override_and_config(tmp_path):
    a = OpenocdIdentify()._args({"openocd": sys.executable, "vid_pid": [0x03EB, 0x2142]}, [])
    assert "cmsis_dap_vid_pid 0x03eb 0x2142" in a
    cfg = tmp_path / "srst.cfg"
    cfg.write_text("# x\n")
    b = OpenocdFlash()._args({"openocd": sys.executable, "config": str(cfg), "speed_khz": 400}, ["init"])
    assert b[1:3] == ["-f", str(cfg)] and "interface/cmsis-dap.cfg" not in b and "adapter speed 400" in b
    with pytest.raises(ParamError, match="not found"):
        OpenocdFlash()._args({"openocd": sys.executable, "config": str(tmp_path / "nope.cfg")}, [])


def test_identify_extras_real(make_ctx, tmp_path, one_probe):
    ctx = make_ctx({"openocd": fake_openocd(tmp_path, IDENT_FULL), "map": {"device_id": "F2", **EXTRA_MAP}}, simulate=False)
    OpenocdIdentify().run(ctx)
    got = {p["test_id"]: p["value"] for p in ctx.proposals}
    # production word: BOOTPROT 7, BODVDD level 8, disable bit 14 = 0
    assert got["F4"] == "7" and got["F5"] == "8" and got["F7"] == "Y" and got["F8"] == "0x015088FF"
    assert got["F6"] == "5A31C0DE0B7A1C2193D04E7F2F8160A5"


def test_identify_extras_missing_words_not_guessed(make_ctx, tmp_path, one_probe):
    ctx = make_ctx({"openocd": fake_openocd(tmp_path, IDENT_OK), "map": {"device_id": "F2", **EXTRA_MAP}}, simulate=False)
    OpenocdIdentify().run(ctx)
    assert {p["test_id"] for p in ctx.proposals} == {"F2"}
    assert sum(lv == "warn" for lv, _ in ctx.logs) >= 2


def test_identify_extras_sim(make_ctx):
    ctx = make_ctx({"map": EXTRA_MAP})
    OpenocdIdentify().run(ctx)
    got = {p["test_id"]: p["value"] for p in ctx.proposals}
    assert got["F4"] == "7" and got["F5"] == "8" and got["F7"] == "Y" and len(got["F6"]) == 32


def test_decode_nvm_user_row():
    d = ocd.decode_nvm_user_row(0x7 | 1 << 14 | 0x21 << 8)
    assert d["bootprot"] == 7 and d["bodvdd_level"] == 0x21 and d["bodvdd_enabled"] == 0


# ---- 17 Oct: probe and target presets (OC-01, OC-02). Only the command lines are checked; no probe is touched.

EXE = {"openocd": sys.executable}


def argv(params, cmds=("init",), cls=OpenocdIdentify):
    return cls()._args({**EXE, **params}, list(cmds))


def test_default_command_line_is_unchanged_for_the_atmel_ice():
    """Backwards compatibility: the exact argument list the protocols of 15-16 Oct produced."""
    assert argv({}, ["init"]) == [sys.executable, "-f", "interface/cmsis-dap.cfg", "-c", "cmsis_dap_vid_pid 0x03eb 0x2141",
                                  "-f", "target/at91samdXX.cfg", "-c", "adapter speed 1000", "-c", "init"]
    assert argv({"probe": "atmel-ice", "target": "sam", "probe_serial": "J1", "speed_khz": 400}, []) == [
        sys.executable, "-f", "interface/cmsis-dap.cfg", "-c", "cmsis_dap_vid_pid 0x03eb 0x2141", "-f", "target/at91samdXX.cfg",
        "-c", "adapter serial J1", "-c", "adapter speed 400"]


def test_generic_cmsis_dap_has_no_usb_restriction_unless_asked():
    a = argv({"probe": "cmsis-dap"})
    assert a[1:3] == ["-f", "interface/cmsis-dap.cfg"] and not any("cmsis_dap_vid_pid" in x for x in a)
    assert "cmsis_dap_vid_pid 0x1234 0x5678" in argv({"probe": "cmsis-dap", "vid_pid": [0x1234, 0x5678]})


def test_stlink_and_jlink_command_lines():
    assert argv({"probe": "stlink"}, [])[:7] == [sys.executable, "-f", "interface/stlink.cfg", "-c", "transport select hla_swd",
                                               "-f", "target/at91samdXX.cfg"]
    j = argv({"probe": "jlink", "probe_serial": "000123"}, [])
    assert j[:3] == [sys.executable, "-f", "interface/jlink.cfg"]
    assert j.index("transport select swd") < j.index("target/at91samdXX.cfg") and "adapter serial 000123" in j


def test_vid_pid_refused_for_probes_without_that_command():
    for pr in ("stlink", "jlink"):
        with pytest.raises(ParamError, match="vid_pid is only implemented"):
            argv({"probe": pr, "vid_pid": [1, 2]})


def test_unknown_probe_and_custom_interface():
    with pytest.raises(ParamError, match="unknown probe"):
        argv({"probe": "bus-pirate"})
    a = argv({"probe": "cmsis-dap", "interface": "interface/my-adapter.cfg"})
    assert a[1:3] == ["-f", "interface/my-adapter.cfg"]


def test_target_presets_aliases_and_custom_script():
    for alias in ("sam", "samc21", "SAMD21", "saml"):
        assert "target/at91samdXX.cfg" in argv({"target": alias})
    assert "target/stm32f4x.cfg" in argv({"target_cfg": "target/stm32f4x.cfg"}, cls=OpenocdFlash)
    with pytest.raises(ParamError, match="not supported yet"):
        argv({"target": "stm32f4"})


def test_identify_for_another_family_fails_clearly(make_ctx):
    ctx = make_ctx({"target_cfg": "target/stm32f4x.cfg"})
    with pytest.raises(ParamError, match="not supported yet"):
        OpenocdIdentify().run(ctx)
    assert ctx.proposals == []


def test_flash_with_custom_target_script_works(make_ctx, tmp_path, one_probe):
    img = tmp_path / "a.elf"
    img.write_bytes(b"\x7fELF")
    ctx = make_ctx({"image": str(img), "target_cfg": "target/stm32f4x.cfg", "probe": "stlink", "map": {"flashed": "F4"},
                    "openocd": fake_openocd(tmp_path, "** Verified OK **")}, simulate=False)
    OpenocdFlash().run(ctx)
    assert ctx.proposals[0]["value"] == "Y"


def test_failure_messages_name_the_probe_in_use():
    assert "ST-LINK" in classify_openocd_output("Error: unable to find a matching CMSIS-DAP device", "stlink")
    assert "J-Link" in classify_openocd_output("Error: unable to find a matching CMSIS-DAP device", "jlink")
    m = classify_openocd_output("Error: device is locked", "cmsis-dap", {"target": "sam", "interface": "interface/x.cfg", "target_cfg": "target/y.cfg"})
    assert "-f interface/x.cfg -f target/y.cfg" in m
    assert "DIO" in classify_openocd_output("Error: Error connecting DP: cannot read IDR")


def test_more_than_one_probe_names_the_preset(make_ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(ocd, "usb_probes", lambda *a: [{"serial": "A", "name": "x"}, {"serial": "B", "name": "x"}])
    with pytest.raises(ProbeError, match="More than one SEGGER J-Link"):
        OpenocdIdentify().run(make_ctx({"probe": "jlink", "openocd": fake_openocd(tmp_path, "")}, simulate=False))


def test_generic_cmsis_dap_is_not_listed_on_usb():
    assert ocd.usb_probes("cmsis-dap") == []


def test_simulator_and_selftest_for_every_preset(make_ctx):
    for pr in ocd.PROBES:
        ctx = make_ctx({"probe": pr, "map": {"device_id": "F2"}})
        OpenocdIdentify().run(ctx)
        assert ctx.proposals[0]["value"] == "0x11001106"
    assert OpenocdIdentify().selftest() and OpenocdFlash().selftest()


def test_preset_labels_never_claim_more_than_was_run():
    for name, spec in ocd.PROBES.items():
        if name != "atmel-ice":
            assert spec["label"].startswith("UNVALIDATED")
    assert "probe-absent" in ocd.PROBES["atmel-ice"]["label"]


def test_no_client_wording_in_the_branch_docs_code_and_tests():
    """OC-06. The words are assembled from pieces so this file does not contain them itself. Scans the plug-in, the doctor,
    their tests, this branch's docs and the doctor page; the integration test is scanned without the two fixture names
    that live in src/polybench/kicad/synth.py (outside this branch's files)."""
    from pathlib import Path
    words = ["stra" + "in", "mcmu" + "rtry", "/Us" + "ers/", "/pri" + "vate/", "ev" + "t1", "atsa" + "mc21e", "samc21" + "e18a", "ads" + "122",
             "mcp" + "2562", "geo" + "rge", "192." + "168"]
    root = Path(ocd.__file__).parents[1]
    repo = Path(ocd.__file__).parents[3]
    files = [Path(ocd.__file__), root / "doctor.py", Path(__file__), repo / "tests/test_doctor.py", repo / "docs/plugins/openocd.md",
             repo / "docs/plugins/OPENOCD_VERIFICATION.md", repo / "site/docs/getting-started/doctor.md"]
    for f in files:
        if not f.exists():          # internal records are not in the public tree
            continue
        low = f.read_text().lower()
        for w in words:
            assert w.lower() not in low, (f.name, w)
    fixture_names = ("stra" + "in_like", "stra" + "in-amp-ev" + "t1", "stra" + "in-like")
    integ = (repo / "tests/kicad/test_integration.py").read_text().lower()
    for n in fixture_names:
        integ = integ.replace(n, "")
    for w in words:
        assert w.lower() not in integ, ("test_integration.py", w)


def test_the_core_stops_only_the_child_it_started(tmp_path, monkeypatch):
    """A bystander process is untouched by a timeout; the OpenOCD this run started is gone."""
    import os
    import subprocess
    import time
    by = subprocess.Popen(["sleep", "30"], start_new_session=True)
    exe = tmp_path / "openocd"
    exe.write_text("#!/bin/sh\nsleep 30\n")
    exe.chmod(0o755)
    monkeypatch.setattr(ocd, "usb_probes", lambda *a: [])
    img = tmp_path / "a.hex"
    img.write_text(":00000001FF\n")
    from polybench.testing import RecordingContext
    ctx = RecordingContext(params={"openocd": str(exe), "image": str(img), "timeout_s": 1}, workdir=tmp_path / "run", simulate=False)
    try:
        with pytest.raises(PluginError, match="timed out"):
            OpenocdFlash().run(ctx)
        assert by.poll() is None                                  # the bystander lives
        assert not (tmp_path / "run" / ocd.PID_FILE).exists()      # the child is gone, so its pid file is gone
        out = subprocess.run(["pgrep", "-f", str(exe)], capture_output=True, text=True).stdout.split()
        assert out == []
    finally:
        by.kill()
        by.wait()


# ---------------------------------------------------------------- D3 Tcl injection (the review's payloads)

PAYLOADS = ["J1; shutdown; exec touch /tmp/from-usb-serial", "J1 [exec echo PWNED]", "J1$x", 'J1" ; exit', "J1{", "J1}", "J1\\n", "J1\nshutdown",
            "J 1"]


@pytest.fixture
def argv_dump(tmp_path, monkeypatch):
    exe = tmp_path / "openocd_dump"
    exe.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {tmp_path}/argv.txt\necho "** Verified OK **"\n')
    exe.chmod(0o755)
    return exe, tmp_path / "argv.txt"


def _flash_ctx(tmp_path, exe, **params):
    from polybench.testing import RecordingContext
    img = tmp_path / "plain.hex"
    img.write_text(":00000001FF\n")
    return RecordingContext(params={"openocd": str(exe), "image": str(params.pop("image", img)), **params},
                            workdir=tmp_path / "run", simulate=False)


@pytest.mark.parametrize("payload", PAYLOADS)
def test_probe_serial_param_with_tcl_syntax_is_refused_and_openocd_never_runs(tmp_path, argv_dump, monkeypatch, payload):
    exe, dump = argv_dump
    monkeypatch.setattr(ocd, "usb_probes", lambda *a: [])
    with pytest.raises(ParamError, match="probe_serial"):
        OpenocdFlash().run(_flash_ctx(tmp_path, exe, probe_serial=payload))
    assert not dump.exists()


@pytest.mark.parametrize("payload", PAYLOADS[:3])
def test_probe_serial_from_usb_with_tcl_syntax_is_refused(tmp_path, argv_dump, monkeypatch, payload):
    exe, dump = argv_dump
    monkeypatch.setattr(ocd, "usb_probes", lambda *a: [{"serial": payload, "name": "evil", "vid": 0x3EB, "pid": 0x2141}])
    with pytest.raises(ParamError, match="probe_serial"):
        OpenocdFlash().run(_flash_ctx(tmp_path, exe))
    with pytest.raises(ParamError, match="probe_serial"):
        OpenocdIdentify().run(_flash_ctx(tmp_path, exe))
    assert not dump.exists()


@pytest.mark.parametrize("name", ["a[exec echo PWNED].elf", "a;shutdown.elf", "a$HOME.elf", 'a"b.elf', "a{b}.elf", "a\\b.elf"])
def test_image_path_with_tcl_syntax_is_refused(tmp_path, argv_dump, monkeypatch, name):
    exe, dump = argv_dump
    monkeypatch.setattr(ocd, "usb_probes", lambda *a: [])
    d = tmp_path / "img dir"
    d.mkdir()
    img = d / name
    try:
        img.write_text("x")
    except OSError:
        pytest.skip("file name not creatable here")
    with pytest.raises(ParamError, match="image path"):
        OpenocdFlash().run(_flash_ctx(tmp_path, exe, image=img))
    assert not dump.exists()


BRACE = "h.cfg}; exec touch /tmp/x; script {h.cfg"


@pytest.mark.parametrize("key", ["interface", "target_cfg"])
def test_interface_and_target_cfg_with_tcl_syntax_are_refused(tmp_path, argv_dump, monkeypatch, key):
    exe, dump = argv_dump
    monkeypatch.setattr(ocd, "usb_probes", lambda *a: [])
    for action in (OpenocdFlash, OpenocdIdentify):
        with pytest.raises(ParamError, match=key):
            action().run(_flash_ctx(tmp_path, exe, **{key: BRACE}))
    assert not dump.exists()


def test_bad_usb_serial_error_blames_the_probe_not_the_operator(tmp_path, argv_dump, monkeypatch):
    exe, dump = argv_dump
    monkeypatch.setattr(ocd, "usb_probes", lambda *a: [{"serial": "J1; shutdown", "name": "x", "vid": 0x3EB, "pid": 0x2141}])
    for action in (OpenocdFlash, OpenocdIdentify):
        with pytest.raises(ParamError, match=r"probe's own USB serial.*Set `probe_serial`"):
            action().run(_flash_ctx(tmp_path, exe))
    assert not dump.exists()


def test_a_plain_serial_and_a_path_with_a_space_still_pass(tmp_path, argv_dump, monkeypatch):
    exe, dump = argv_dump
    monkeypatch.setattr(ocd, "usb_probes", lambda *a: [])
    d = tmp_path / "img dir"
    d.mkdir()
    img = d / "app-1.0_rc.hex"
    img.write_text(":00000001FF\n")
    OpenocdFlash().run(_flash_ctx(tmp_path, exe, image=img, probe_serial="J41800012345"))
    args = dump.read_text().splitlines()
    assert "adapter serial J41800012345" in args and any(a.startswith('program "') and "img dir" in a for a in args)


def test_interface_with_spaces_stays_one_argument(tmp_path, argv_dump, monkeypatch):
    exe, dump = argv_dump
    monkeypatch.setattr(ocd, "usb_probes", lambda *a: [])
    OpenocdFlash().run(_flash_ctx(tmp_path, exe, probe="cmsis-dap", interface="interface/x.cfg -c shutdown"))
    assert "interface/x.cfg -c shutdown" in dump.read_text().splitlines()


# ---------------------------------------------------------------- D5 surviving OpenOCD

def _fake_survivor(tmp_path):
    """A process that looks like an OpenOCD left by a killed core, with its pid file in another run folder."""
    import json
    import subprocess
    (tmp_path / "surv").mkdir()
    exe = tmp_path / "surv" / "openocd"
    exe.write_text("#!/bin/sh\nsleep 30\n")
    exe.chmod(0o755)
    proc = subprocess.Popen([str(exe), "-f", "interface/cmsis-dap.cfg"], start_new_session=True)
    old = tmp_path / "pb-data" / "batch1" / "runs" / "0001_V1_flash"
    old.mkdir(parents=True)
    (old / ocd.PID_FILE).write_text(json.dumps({"pid": proc.pid, "name": "openocd", "started": "x"}))
    return proc, old


def test_pid_file_exists_while_openocd_runs_and_is_removed_after(tmp_path, monkeypatch):
    exe = tmp_path / "openocd"
    exe.write_text(f'#!/bin/sh\ncp "{tmp_path}/run/{ocd.PID_FILE}" "{tmp_path}/seen.json"\necho "** Verified OK **"\n')
    exe.chmod(0o755)
    monkeypatch.setattr(ocd, "usb_probes", lambda *a: [])
    OpenocdFlash().run(_flash_ctx(tmp_path, exe))
    import json
    seen = json.loads((tmp_path / "seen.json").read_text())
    assert seen["pid"] > 1 and seen["name"] == "openocd"
    assert not (tmp_path / "run" / ocd.PID_FILE).exists()


def test_next_run_and_safe_state_report_a_survivor_and_never_kill_it(tmp_path, monkeypatch):
    survivor, old = _fake_survivor(tmp_path)
    try:
        exe = fake_openocd(tmp_path, "Error: unable to find a matching CMSIS-DAP device", rc=1)
        monkeypatch.setattr(ocd, "usb_probes", lambda *a: [])
        ctx = _flash_ctx(tmp_path, exe)
        with pytest.raises(ProbeError) as e:
            OpenocdFlash().run(ctx)
        for text in (str(e.value), " ".join(t for lvl, t in ctx.logs if lvl == "warn")):
            assert f"pid {survivor.pid}" in text and "0001_V1_flash" in text and f"kill {survivor.pid}" in text
            assert "will not stop it" in text
        st = OpenocdFlash().safe_state()
        assert f"pid {survivor.pid}" in st and "OpenOCD" in st
        assert survivor.poll() is None                             # still alive: nothing was killed
    finally:
        survivor.kill()
        survivor.wait()


def test_a_stale_pid_file_or_a_reused_pid_is_not_reported(tmp_path):
    import json
    import subprocess
    other = subprocess.Popen(["sleep", "30"])
    try:
        for pid in (other.pid, 999999):
            d = tmp_path / "pb-data" / f"b{pid}" / "runs" / "0001_x"
            d.mkdir(parents=True)
            (d / ocd.PID_FILE).write_text(json.dumps({"pid": pid}))
        assert ocd.surviving_openocd() == []
        assert "no OpenOCD process from an earlier run" in OpenocdFlash().safe_state()
    finally:
        other.kill()
        other.wait()


def test_a_killed_core_leaves_a_pid_file_that_the_next_run_reports(tmp_path):
    """The review's scenario: SIGKILL the core while OpenOCD runs; the child survives, and the next run names it."""
    import os
    import signal
    import subprocess
    import sys
    import time
    exe = tmp_path / "openocd"
    exe.write_text("#!/bin/sh\nsleep 30\n")
    exe.chmod(0o755)
    img = tmp_path / "a.hex"
    img.write_text(":00000001FF\n")
    code = ("import polybench.plugins.openocd as o; from polybench.testing import RecordingContext as C; "
            "o.usb_probes=lambda *a: []; "
            f"o.OpenocdFlash().run(C(params={{'openocd': {str(exe)!r}, 'image': {str(img)!r}, 'timeout_s': 60}}, "
            f"workdir={str(tmp_path / 'pb-data' / 'batch1' / 'runs' / '0001_V1_flash')!r}, simulate=False))")
    core = subprocess.Popen([sys.executable, "-c", code], env={**os.environ, "POLYBENCH_DATA": str(tmp_path / "pb-data")})
    pidfile = tmp_path / "pb-data" / "batch1" / "runs" / "0001_V1_flash" / ocd.PID_FILE
    for _ in range(40):
        if pidfile.exists():
            break
        time.sleep(0.25)
    assert pidfile.exists()
    import json
    child = json.loads(pidfile.read_text())["pid"]
    try:
        core.send_signal(signal.SIGKILL)
        core.wait()
        found = ocd.surviving_openocd()
        assert [o["pid"] for o in found] == [child]
    finally:
        try:
            os.killpg(child, signal.SIGKILL)
        except ProcessLookupError:
            pass


# ---------------------------------------------------------------- D6 / D8

def test_locked_hint_is_sam_specific_only_for_sam():
    sam = classify_openocd_output("Error: device is locked", "atmel-ice", {})
    assert "at91samd chip-erase" in sam
    assert "at91samd" in classify_openocd_output("Error: device is locked", "atmel-ice", {"target": "samd21"})
    for params in ({"target": "stm32f4", "target_cfg": "target/stm32f4x.cfg"}, {"target_cfg": "target/stm32f4x.cfg"}):
        other = classify_openocd_output("Error: device is locked", "stlink", params)
        assert "at91samd" not in other and "locked" in other and "vendor" in other


def test_target_cfg_overrides_an_unknown_target_name(tmp_path, argv_dump, monkeypatch):
    exe, dump = argv_dump
    monkeypatch.setattr(ocd, "usb_probes", lambda *a: [])
    OpenocdFlash().run(_flash_ctx(tmp_path, exe, target="stm32f4", target_cfg="target/stm32f4x.cfg"))
    args = dump.read_text().splitlines()
    assert "target/stm32f4x.cfg" in args and "target/at91samdXX.cfg" not in args
    with pytest.raises(ParamError, match="not supported yet"):                       # the id read stays unsupported
        OpenocdIdentify().run(_flash_ctx(tmp_path, exe, target="stm32f4", target_cfg="target/stm32f4x.cfg"))
    with pytest.raises(ParamError, match="not supported yet"):                       # no script: still an error
        OpenocdFlash().run(_flash_ctx(tmp_path, exe, target="stm32f4"))


MAP_TXT = """\
 .bss.g_selftest
                0x0000000020000117       0x28 CMakeFiles/selftest.dir/src/main_selftest.c.obj
                0x0000000020000117                g_selftest
 .bss.g_other
                0x0000000020000140                g_selftest_extra
"""


def test_read_ram_symbol_lookup(tmp_path):
    from polybench.plugins.openocd import OpenocdReadRam
    m = tmp_path / "x.map"
    m.write_text(MAP_TXT)
    assert OpenocdReadRam._symbol(str(m), "g_selftest") == 0x20000117  # exact name only, not g_selftest_extra
    with pytest.raises(ParamError):
        OpenocdReadRam._symbol(str(m), "g_missing")


def test_read_ram_simulate_proposes_bits_and_bytes(tmp_path):
    from polybench.plugins.openocd import OpenocdReadRam
    from polybench.testing import RecordingContext
    m = tmp_path / "x.map"
    m.write_text(MAP_TXT)
    ctx = RecordingContext(simulate=True, params={
        "map_file": str(m), "symbol": "g_selftest", "length": 40, "ready": {"byte": 34, "not": 0},
        "fields": [{"test": "F82", "byte": 36, "bit": 0}, {"test": "F89", "byte": 36, "bit": 7}, {"test": "F90", "byte": 34}]})
    OpenocdReadRam().run(ctx)
    got = {p["test_id"]: p["value"] for p in ctx.proposals}
    assert got == {"F82": "1", "F89": "1", "F90": "1"}


def _ram_ctx(fields, length=16):
    from polybench.testing import RecordingContext
    return RecordingContext(simulate=True, params={"address": "0x20000000", "length": length, "fields": fields})


def test_read_ram_u16_le(tmp_path):
    from polybench.plugins.openocd import OpenocdReadRam
    ctx = _ram_ctx([{"test": "V01", "byte": 4, "width": 2, "sim": 3300}])
    OpenocdReadRam().run(ctx)
    assert ctx.proposals[0]["value"] == "3300"


def test_read_ram_i32_negative():
    from polybench.plugins.openocd import OpenocdReadRam
    ctx = _ram_ctx([{"test": "V04", "byte": 8, "width": 4, "signed": True, "sim": -12345}])
    OpenocdReadRam().run(ctx)
    assert ctx.proposals[0]["value"] == "-12345"


def test_read_ram_scale_offset_decimals():
    from polybench.plugins.openocd import OpenocdReadRam
    ctx = _ram_ctx([{"test": "V03", "byte": 0, "width": 2, "signed": True, "scale": 0.01, "offset": 1.5, "decimals": 2, "sim": -3.25},
                    {"test": "V09", "byte": 2, "width": 2, "scale": 0.5, "sim": 10}])
    OpenocdReadRam().run(ctx)
    assert [p["value"] for p in ctx.proposals] == ["-3.25", "10"]


def test_read_ram_invalid_raw_proposes_nothing():
    from polybench.plugins.openocd import OpenocdReadRam
    ctx = _ram_ctx([{"test": "V07", "byte": 0, "width": 2, "invalid": 0},
                    {"test": "V08", "byte": 2, "width": 2, "invalid": 65535, "sim": 7}])
    OpenocdReadRam().run(ctx)
    assert [p["test_id"] for p in ctx.proposals] == ["V08"]
    assert any(lvl == "warn" and "V07" in t for lvl, t in ctx.logs)


def test_read_ram_big_endian_and_bounds():
    from polybench.plugins.openocd import OpenocdReadRam
    ctx = _ram_ctx([{"test": "X", "byte": 0, "width": 2, "endian": "big", "sim": 258}])
    OpenocdReadRam().run(ctx)
    assert ctx.proposals[0]["value"] == "258"
    with pytest.raises(ParamError):
        OpenocdReadRam().run(_ram_ctx([{"test": "X", "byte": 15, "width": 2}]))
    with pytest.raises(ParamError):
        OpenocdReadRam().run(_ram_ctx([{"test": "X", "byte": 0, "width": 3}]))
