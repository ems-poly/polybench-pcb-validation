"""polybench doctor: read-only preflight. Everything hardware-facing is replaced; nothing here touches USB, CAN or a LAN."""
import hashlib
import textwrap
from pathlib import Path

import pytest

from polybench import doctor
import pytest
from polybench.doctor import FAIL, INFO, OK, WARN, Check

@pytest.fixture(autouse=True)
def _scratch_data_dir(tmp_path_factory, monkeypatch):
    """No test here may look at the real data folder, even read-only."""
    monkeypatch.setenv("POLYBENCH_DATA", str(tmp_path_factory.mktemp("pbdata") / "data"))


IOREG = textwrap.dedent('''\
    +-o Root  <class IORegistryEntry>
      +-o VER1:PEAK    @01100000  <class AppleUSBDevice>
      |   "idProduct" = 12
      |   "USB Product Name" = "VER1:PEAK"
      |   "idVendor" = 3186
      +-o Atmel-ICE CMSIS-DAP@14100000  <class IOUSBHostDevice>
      |   "idVendor" = 1003
      |   "idProduct" = 8513
      |   "USB Serial Number" = "J41800012345"
      +-o Keyboard@1
          "idVendor" = 1452
          "idProduct" = 600
    ''')


def test_parse_usb_devices_separates_devices():
    d = doctor.parse_usb_devices(IOREG)
    by = {x["vid"]: x for x in d}
    assert by[3186]["pid"] == 12 and "serial" not in by[3186]
    assert by[1003]["pid"] == 0x2141 and by[1003]["serial"] == "J41800012345"
    assert by[1452]["pid"] == 600


def test_atmel_ice_checks():
    d = doctor.parse_usb_devices(IOREG)
    assert doctor.check_atmel_ice(d).status == OK
    assert doctor.check_atmel_ice([x for x in d if x["vid"] != 1003]).status == FAIL
    assert doctor.check_atmel_ice(None).status == WARN
    two = d + [{"name": "ICE2", "vid": 1003, "pid": 0x2141, "serial": "J2"}]
    assert doctor.check_atmel_ice(two).status == WARN


def test_pcan_held_names_holder_and_never_opens(monkeypatch):
    monkeypatch.setattr(doctor, "pcan_holders", lambda: [("66893", "soak_overnight.py --run B3")])
    opened = []
    monkeypatch.setitem(__import__("sys").modules, "can", type("C", (), {"Bus": lambda *a, **k: opened.append(1)}))
    c = doctor.check_pcan(doctor.parse_usb_devices(IOREG), open_test=True)
    assert c.status == FAIL and "pid 66893" in c.detail and "soak_overnight.py" in c.detail
    assert "never stops" in c.hint and opened == []


def test_pcan_absent_and_free(monkeypatch):
    monkeypatch.setattr(doctor, "pcan_holders", lambda: [])
    d = doctor.parse_usb_devices(IOREG)
    assert doctor.check_pcan([x for x in d if x["vid"] != 3186]).status == FAIL
    assert doctor.check_pcan(d, open_test=False).status == OK


def test_pcan_free_opens_listen_only_and_releases(monkeypatch):
    monkeypatch.setattr(doctor, "pcan_holders", lambda: [])
    calls = {}

    class Bus:
        def __init__(self, **kw): calls["kw"] = kw
        def shutdown(self): calls["down"] = True

    class State: PASSIVE = "passive"
    import sys
    monkeypatch.setitem(sys.modules, "can", type("C", (), {"Bus": Bus, "BusState": State}))
    c = doctor.check_pcan(doctor.parse_usb_devices(IOREG), open_test=True)
    assert c.status == OK and calls["kw"]["state"] == "passive" and calls["down"] is True


def test_n6705b_unreachable_is_amber_not_red():
    c = doctor.check_n6705b("127.0.0.1", timeout=0.2)     # nothing listens on 5025 here
    assert c.status == WARN and "network" in c.hint


def test_fusion_wrong_document_warns(monkeypatch):
    import polybench.plugins.fusion as fz

    class B:
        def __init__(self, **k): pass
        def status(self): return {"connected": True, "document": "demo-board-rev1", "editor": "board", "detail": "57 parts"}
    monkeypatch.setattr(fz, "FusionBridge", B)
    assert doctor.check_fusion("motor").status == WARN
    assert doctor.check_fusion("demo").status == OK
    assert doctor.check_fusion(None).status == OK


def _proto(tmp_path, image, sha):
    from polybench.protocol import load
    image = Path(image).as_posix()                       # a Windows path inside YAML double quotes would read \U as an escape
    (tmp_path / "p.pcbv.yaml").write_text(textwrap.dedent(f'''\
        schema: pcbv.protocol/2
        design: {{name: T, revision: "1"}}
        batch: {{default_boards: 1, label_prefix: S}}
        steps:
          - id: function
            title: Function
            applies_to: all
            sections:
              - id: s
                title: S
                actions:
                  - id: flash
                    title: Flash
                    plugin: openocd.flash
                    params: {{image: "{image}", map: {{image_sha256: F1}}}}
                    proposes: [F1]
                tests:
                  - id: F1
                    title: Hash
                    kind: text
                    limit: {{class: hard, equals: "{sha}", source: {{type: design_calc, doc: x}}}}
        '''), encoding="utf-8")
    return load(tmp_path / "p.pcbv.yaml")


def test_images_match_manifest_and_protocol(tmp_path):
    img = tmp_path / "a.hex"
    img.write_bytes(b"firmware")
    h = hashlib.sha256(b"firmware").hexdigest()
    (tmp_path / "MANIFEST.txt").write_text(f"{h}  build/a.hex\n", encoding="utf-8")
    res = doctor.check_images(_proto(tmp_path, img, h))
    assert [c.status for c in res] == [OK]


def test_images_mismatch_and_missing(tmp_path):
    img = tmp_path / "a.hex"
    img.write_bytes(b"firmware")
    h = hashlib.sha256(b"firmware").hexdigest()
    (tmp_path / "MANIFEST.txt").write_text(f"{'0' * 64}  build/a.hex\n", encoding="utf-8")
    res = doctor.check_images(_proto(tmp_path, img, h))
    assert res[0].status == FAIL and "MANIFEST.txt" in res[0].detail
    res = doctor.check_images(_proto(tmp_path, tmp_path / "gone.hex", h))
    assert res[0].status == FAIL and "gone.hex" in res[0].detail
    res = doctor.check_images(_proto(tmp_path, img, "f" * 64))
    assert any("protocol's expected hash" in c.detail for c in res)


def test_render_and_exit_code():
    res = [Check(OK, "a", "fine"), Check(WARN, "b", "meh", "do x"), Check(FAIL, "c", "bad", "fix y")]
    out = doctor.render(res, color=False)
    assert "[OK  ] a" in out and "[WARN] b" in out and "[FAIL] c" in out and "-> fix y" in out
    assert "1 ok, 0 info, 1 warn, 1 fail. NOT READY" in out
    assert "READY\n" not in doctor.render([Check(WARN, "b", "x")], False).replace("READY with warnings", "")


def test_run_checks_never_raises_when_a_check_crashes(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "usb_devices", lambda: [])
    monkeypatch.setattr(doctor, "check_openocd", lambda probes=None, severity=None: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(doctor, "pcan_holders", lambda: [])
    res = doctor.run_checks(str(_caps_proto(tmp_path, ["openocd.identify"])), open_pcan=False)
    assert any(c.status == FAIL and "boom" in c.detail for c in res)


def test_param_files_checked(tmp_path):
    from polybench.protocol import load
    good = tmp_path / "p.toml"
    good.write_text("x=1\n", encoding="utf-8")

    def proto(profile):
        (tmp_path / "q.pcbv.yaml").write_text(textwrap.dedent(f'''\
            schema: pcbv.protocol/2
            design: {{name: T, revision: "1"}}
            batch: {{default_boards: 1, label_prefix: S}}
            steps:
              - id: powered
                title: P
                applies_to: all
                sections:
                  - id: s
                    title: S
                    actions:
                      - {{id: fp, title: FP, plugin: n6705b.first_power, params: {{profile: "{profile}"}}, proposes: [P1]}}
                    tests:
                      - {{id: P1, title: V, kind: number, limit: {{class: record}}}}
            '''), encoding="utf-8")
        return load(tmp_path / "q.pcbv.yaml")
    assert doctor.check_param_files(proto(good)).status == OK
    assert doctor.check_param_files(proto(tmp_path / "gone.toml")).status == FAIL


# ---------------------------------------------------------------- capability filter (17 Oct)

def _caps_proto(tmp_path, plugins, caps=(), params=None, name="c.pcbv.yaml"):
    """A protocol whose function step runs one action per plugin name."""
    acts = "\n".join(f"                  - {{id: a{i}, title: A{i}, plugin: {pl}, params: {params or {}}, proposes: []}}"
                     for i, pl in enumerate(plugins))
    capy = "capabilities:\n" + "\n".join(f"  - {{name: {k}, kind: {k}, optional: {str(o).lower()}}}" for k, o in caps) if caps else ""
    (tmp_path / name).write_text(textwrap.dedent("""\
        schema: pcbv.protocol/2
        design: {name: T, revision: "1"}
        batch: {default_boards: 1, label_prefix: B}
        steps:
          - id: function
            title: Function
            applies_to: all
            sections:
              - id: s
                title: S
                actions:
        """) + acts + "\n" + capy + "\n", encoding="utf-8")
    return tmp_path / name


@pytest.fixture
def quiet(monkeypatch):
    """No USB probe, no PCAN holder; records every instrument query."""
    q = {"n6705b": 0, "fusion": 0, "pcan_open": 0}
    monkeypatch.setattr(doctor, "usb_devices", lambda: [])
    monkeypatch.setattr(doctor, "pcan_holders", lambda: [])
    monkeypatch.setattr(doctor, "check_n6705b", lambda h, timeout=3.0: q.__setitem__("n6705b", q["n6705b"] + 1) or Check(OK, "N6705B power analyzer", "x"))
    monkeypatch.setattr(doctor, "check_fusion", lambda e: q.__setitem__("fusion", q["fusion"] + 1) or Check(OK, "Fusion MCP", "x"))

    class Bus:                                   # counts real opens of the CAN adapter
        def __init__(self, **kw): q["pcan_open"] += 1
        def shutdown(self): pass

    class State: PASSIVE = "passive"
    import sys
    monkeypatch.setitem(sys.modules, "can", type("C", (), {"Bus": Bus, "BusState": State}))
    return q


def names(res):
    return [c.name for c in res]


def test_demo_style_protocol_has_no_programmer_pcan_or_instrument_lines(tmp_path, quiet):
    res = doctor.run_checks(str(_caps_proto(tmp_path, ["demo.measure"])), open_pcan=False)
    text = " ".join(names(res)).lower()
    assert "probe" not in text and "pcan" not in text and "openocd" not in text and "n6705b" not in text and "fusion" not in text
    assert not any(c.status == FAIL for c in res), [c for c in res if c.status == FAIL]
    assert quiet == {"n6705b": 0, "fusion": 0, "pcan_open": 0}


def test_openocd_protocol_checks_its_probe_only(tmp_path, quiet):
    res = doctor.run_checks(str(_caps_proto(tmp_path, ["openocd.flash"], params={"probe": "stlink"})), open_pcan=False)
    assert "probe ST-LINK" in names(res) and "probe Atmel-ICE" not in names(res)
    assert next(c for c in res if c.name == "probe ST-LINK").status == FAIL      # none on USB; programmer is required
    assert not any("PCAN" in c.name for c in res)


def test_optional_programmer_capability_is_amber_not_red(tmp_path, quiet):
    p = _caps_proto(tmp_path, ["openocd.flash"], caps=[("programmer", True)])
    res = doctor.run_checks(str(p), open_pcan=False)
    assert next(c for c in res if c.name == "probe Atmel-ICE").status == WARN


def test_n6705b_is_opt_in(tmp_path, quiet):
    p = _caps_proto(tmp_path, ["n6705b.measure"])
    res = doctor.run_checks(str(p), open_pcan=False)
    c = next(c for c in res if c.name == "N6705B power analyzer")
    assert c.status == INFO and "not queried" in c.detail and quiet["n6705b"] == 0
    doctor.run_checks(str(p), n6705b_host="198.51.100.7", open_pcan=False)
    assert quiet["n6705b"] == 1
    doctor.run_checks(str(p), open_pcan=False, query_n6705b=True)
    assert quiet["n6705b"] == 2
    # an unreferenced instrument is never queried, even with the flag
    doctor.run_checks(str(_caps_proto(tmp_path, ["demo.measure"], name="d.pcbv.yaml")), open_pcan=False, query_n6705b=True)
    assert quiet["n6705b"] == 2


def test_n6705b_has_no_builtin_host(monkeypatch):
    monkeypatch.delenv("POLYBENCH_N6705B_HOST", raising=False)
    monkeypatch.setattr(doctor, "_configured_host", lambda: None)
    c = doctor.check_n6705b(None)
    assert c.status == WARN and "nothing was queried" in c.detail


def test_pcan_only_for_can_protocols(tmp_path, quiet):
    peak = [{"name": "PEAK", "vid": 0x0C72, "pid": 12}]
    doctor.usb_devices = lambda: peak
    res = doctor.run_checks(str(_caps_proto(tmp_path, ["pcan.capture"])), open_pcan=False)
    assert "PCAN adapter" in names(res) and not any("probe" in n for n in names(res))
    assert quiet["pcan_open"] == 0                                   # --no-open: never opened
    doctor.run_checks(str(_caps_proto(tmp_path, ["pcan.capture"])), open_pcan=True)
    assert quiet["pcan_open"] == 1                                   # opened once, listen-only
    doctor.run_checks(str(_caps_proto(tmp_path, ["demo.measure"], name="d.pcbv.yaml")), open_pcan=True)
    assert quiet["pcan_open"] == 1                                   # a protocol without pcan.* never opens it


def test_ecad_checked_only_with_a_capability(tmp_path, quiet):
    doctor.run_checks(str(_caps_proto(tmp_path, ["demo.measure"])), open_pcan=False)
    assert quiet["fusion"] == 0
    doctor.run_checks(str(_caps_proto(tmp_path, ["demo.measure"], caps=[("ecad", True)], name="e.pcbv.yaml")), open_pcan=False)
    assert quiet["fusion"] == 1


def test_no_protocol_lists_installed_without_red_hardware_lines(monkeypatch, quiet, tmp_path):
    monkeypatch.setattr(doctor, "check_data_dir", lambda d=None: Check(OK, "data folder", "x"))
    monkeypatch.setattr(doctor, "check_port", lambda p: Check(OK, "port", "free"))
    res = doctor.run_checks(None, open_pcan=False)
    assert not any(c.status == FAIL for c in res), [c for c in res if c.status == FAIL]
    assert any(c.name == "protocol" and c.status == INFO for c in res)
    assert quiet == {"n6705b": 0, "fusion": 0, "pcan_open": 0}


def test_expect_doc_has_no_default(monkeypatch, tmp_path, quiet, capsys):
    """No --expect-doc: an open ECAD document of any name is fine."""
    monkeypatch.setattr(doctor, "check_fusion", lambda e: Check(OK if e is None else WARN, "Fusion MCP", f"expect={e}"))
    p = _caps_proto(tmp_path, ["demo.measure"], caps=[("ecad", True)])
    doctor.main(["--protocol", str(p), "--no-open", "--data-dir", str(tmp_path / "d")])
    assert "expect=None" in capsys.readouterr().out


def test_probe_checks():
    d = doctor.parse_usb_devices(IOREG)
    assert doctor.check_probe(d, "atmel-ice").status == OK
    assert doctor.check_probe(d, "jlink").status == FAIL
    assert doctor.check_probe(d, "cmsis-dap").status == INFO
    assert doctor.check_probe(d, "atmel-ice", serial="Jnope").status == WARN
    assert doctor.check_probe(d, "jlink", severity=WARN).status == WARN


# ---------------------------------------------------------------- D1 / D2 optional capability is amber for every check

import importlib.metadata as _md


def _missing_packages(monkeypatch):
    real = _md.version
    monkeypatch.setattr(doctor.md, "version", lambda n: (_ for _ in ()).throw(_md.PackageNotFoundError(n)) if n in
                        ("python-can", "polystage-bench", "opencv-python", "cantools") else real(n))


CASES = [("pcan.capture", "bus_adapter", "package python-can"), ("n6705b.measure", "dc_source", "package polystage-bench"),
         ("camera.snapshot", "camera", "package opencv-python")]


@pytest.mark.parametrize("plugin,kind,pkg", CASES)
def test_missing_package_is_amber_when_its_capability_is_optional_and_red_when_required(tmp_path, quiet, monkeypatch, plugin, kind, pkg):
    _missing_packages(monkeypatch)
    for optional, want in ((True, WARN), (False, FAIL)):
        res = doctor.run_checks(str(_caps_proto(tmp_path, [plugin], caps=[(kind, optional)], name=f"{kind}{optional}.pcbv.yaml")), open_pcan=False)
        assert next(c for c in res if c.name == pkg).status == want, (pkg, optional)
        assert not any(c.status == FAIL for c in res if c.name.startswith("package")) or not optional


def test_optional_dc_source_with_no_library_is_not_red_and_not_not_ready(tmp_path, quiet, monkeypatch):
    """The private-protocol shape: n6705b.* actions, dc_source optional, polystage-bench not installed."""
    _missing_packages(monkeypatch)
    monkeypatch.setattr(doctor, "check_data_dir", lambda d=None: Check(OK, "data folder", "x"))
    monkeypatch.setattr(doctor, "check_port", lambda p: Check(OK, "port", "free"))
    p = _caps_proto(tmp_path, ["n6705b.first_power", "n6705b.measure"], caps=[("dc_source", True)])
    res = doctor.run_checks(str(p), open_pcan=False)
    assert next(c for c in res if c.name == "package polystage-bench").status == WARN
    assert not any(c.status == FAIL for c in res), [c for c in res if c.status == FAIL]
    assert "NOT READY" not in doctor.render(res, False)


def test_pcan_checks_follow_the_capability(monkeypatch):
    monkeypatch.setattr(doctor, "pcan_holders", lambda: [])
    assert doctor.check_pcan([], severity=WARN).status == WARN                       # adapter absent
    assert doctor.check_pcan([], severity=FAIL).status == FAIL
    peak = [{"vid": 0x0C72, "pid": 12}]
    monkeypatch.setattr(doctor, "pcan_holders", lambda: [("42", "other.py")])
    assert doctor.check_pcan(peak, severity=WARN).status == WARN                     # held by someone
    monkeypatch.setattr(doctor, "pcan_holders", lambda: [])
    import sys

    class Bus:
        def __init__(self, **kw): raise OSError("busy")
    monkeypatch.setitem(sys.modules, "can", type("C", (), {"Bus": Bus, "BusState": type("S", (), {"PASSIVE": 1})}))
    assert doctor.check_pcan(peak, severity=WARN).status == WARN                     # open failed
    assert doctor.check_pcan(peak).status == FAIL


def test_openocd_checks_follow_the_capability(monkeypatch):
    from polybench.plugins import openocd as o
    from polybench.plugins._util import PluginError
    monkeypatch.setattr(o, "find_openocd", lambda p=None: (_ for _ in ()).throw(PluginError("openocd is not installed")))
    assert doctor.check_openocd([], severity=WARN).status == WARN
    assert doctor.check_openocd([]).status == FAIL


def test_every_optional_hardware_line_is_amber_through_run_checks(tmp_path, quiet, monkeypatch):
    """One protocol that uses programmer, bus adapter and dc source, all optional: nothing red, whatever is missing."""
    from polybench.plugins import openocd as o
    from polybench.plugins._util import PluginError
    _missing_packages(monkeypatch)
    monkeypatch.setattr(o, "find_openocd", lambda p=None: (_ for _ in ()).throw(PluginError("openocd is not installed")))
    monkeypatch.setattr(doctor, "check_data_dir", lambda d=None: Check(OK, "data folder", "x"))
    monkeypatch.setattr(doctor, "check_port", lambda p: Check(OK, "port", "free"))
    img = tmp_path / "gone.hex"
    p = _caps_proto(tmp_path, ["openocd.flash", "pcan.capture", "n6705b.measure"],
                    caps=[("programmer", True), ("bus_adapter", True), ("dc_source", True)],
                    params={"image": str(img), "config": str(tmp_path / "gone.cfg"), "dbc": str(tmp_path / "gone.dbc"), "profile": str(tmp_path / "gone.toml")})
    monkeypatch.delitem(__import__("sys").modules, "can")        # the real python-can for the DBC loader
    res = doctor.run_checks(str(p), open_pcan=False)
    assert not [c for c in res if c.status == FAIL], [(c.name, c.detail) for c in res if c.status == FAIL]
    for n in ("OpenOCD", "probe Atmel-ICE", "PCAN adapter", "firmware images", "profile / config files", "package python-can"):
        assert next(c for c in res if c.name == n).status == WARN, n
    req = doctor.run_checks(str(_caps_proto(tmp_path, ["openocd.flash", "pcan.capture"], params={"image": str(img)}, name="r.pcbv.yaml")), open_pcan=False)
    assert {c.name for c in req if c.status == FAIL} >= {"OpenOCD", "probe Atmel-ICE", "PCAN adapter", "firmware images"}


# ---------------------------------------------------------------- D4 read-only data folder

def test_data_folder_check_creates_and_writes_nothing(tmp_path):
    missing = tmp_path / "not-yet"
    c = doctor.check_data_dir(missing)
    assert c.status == INFO and "does not exist" in c.detail and not missing.exists()
    existing = tmp_path / "there"
    existing.mkdir()
    c = doctor.check_data_dir(existing)
    assert c.status == OK and list(existing.iterdir()) == []


def test_data_folder_unwritable_is_red(tmp_path):
    import os
    d = tmp_path / "ro"
    d.mkdir()
    d.chmod(0o500)
    try:
        if os.access(d, os.W_OK):
            pytest.skip("running as a user that ignores permissions")
        assert doctor.check_data_dir(d).status == FAIL
    finally:
        d.chmod(0o700)


def test_data_dir_flag_and_env(tmp_path, monkeypatch, quiet, capsys):
    target = tmp_path / "flagged"
    doctor.main(["--no-open", "--data-dir", str(target)])
    assert str(target) in capsys.readouterr().out and not target.exists()
    monkeypatch.setenv("POLYBENCH_DATA", str(tmp_path / "from-env"))
    assert str(tmp_path / "from-env") in doctor.check_data_dir().detail


def test_doctor_run_writes_nothing_anywhere_in_the_data_folder(tmp_path, quiet):
    d = tmp_path / "data"
    d.mkdir()
    doctor.run_checks(None, open_pcan=False, data_dir=str(d))
    assert list(d.iterdir()) == []


# ---------------------------------------------------------------- D7 vid_pid

def test_vid_pid_of_the_action_is_what_the_probe_check_wants(tmp_path, quiet):
    edbg = [{"name": "EDBG", "vid": 0x3EB, "pid": 0x2111, "serial": "E1"}]
    assert doctor.check_probe(edbg, "atmel-ice").status == WARN                       # default PID 2141 not present
    assert doctor.check_probe(edbg, "atmel-ice", vid_pid=(0x3EB, 0x2111)).status == OK
    assert doctor.check_probe(edbg, "atmel-ice", vid_pid=(0x3EB, 0x2141)).status != OK
    assert doctor.check_probe(edbg, "cmsis-dap", vid_pid=(0x3EB, 0x2111)).status == OK       # a generic probe given an id is checked
    assert doctor.check_probe([], "cmsis-dap", vid_pid=(0x3EB, 0x2111)).status == FAIL
    p = _caps_proto(tmp_path, ["openocd.flash"], params={"probe": "atmel-ice", "vid_pid": [0x3EB, 0x2111]})
    assert [(a, b, c) for a, b, c in doctor.Uses(__import__("polybench.protocol", fromlist=["load"]).load(p)).probes()] == [("atmel-ice", None, (0x3EB, 0x2111))]
    doctor.usb_devices = lambda: edbg
    res = doctor.run_checks(str(p), open_pcan=False)
    assert next(c for c in res if c.name == "probe Atmel-ICE").status == OK


# ---------------------------------------------------------------- C4 images of any plug-in

def test_any_action_with_an_image_param_gets_the_image_checks(tmp_path):
    img = tmp_path / "board.factory.bin"
    img.write_bytes(b"esp-image")
    good = hashlib.sha256(b"esp-image").hexdigest()
    (tmp_path / "MANIFEST.txt").write_text(f"{good}  build/board.factory.bin\n", encoding="utf-8")

    def proto(image, sha):
        return _caps_proto(tmp_path, ["esptool.flash"], params={"image": str(image), "offset": "0x0", "sha256": sha,
                                                               "map": {"flashed": "F1"}}, name="e.pcbv.yaml")
    from polybench.protocol import load
    imgs = doctor.protocol_images(load(proto(img, good)))
    assert imgs and imgs[0]["plugin"] == "esptool.flash" and imgs[0]["expected"] == good
    ok = doctor.check_images(load(proto(img, good)))
    assert [c.status for c in ok] == [OK] and "1 images present" in ok[0].detail
    bad = doctor.check_images(load(proto(img, "0" * 64)))
    assert bad[0].status == FAIL and "expected hash" in bad[0].detail
    gone = doctor.check_images(load(proto(tmp_path / "gone.bin", good)))
    assert gone[0].status == FAIL and "gone.bin" in gone[0].detail
    (tmp_path / "MANIFEST.txt").write_text(f"{'1' * 64}  build/board.factory.bin\n", encoding="utf-8")
    assert "MANIFEST.txt" in doctor.check_images(load(proto(img, good)))[0].detail


def test_image_hash_can_come_from_the_image_sha256_test_for_any_plugin(tmp_path):
    img = tmp_path / "a.bin"
    img.write_bytes(b"x")
    p = _proto(tmp_path, img, hashlib.sha256(b"x").hexdigest())
    assert doctor.protocol_images(p)[0]["expected"] == hashlib.sha256(b"x").hexdigest()


def _sha_proto(tmp_path, sha):
    from polybench.protocol import load
    img = tmp_path / "a.bin"
    img.write_bytes(b"x")
    return load(_caps_proto(tmp_path, ["esptool.flash"], params={"image": str(img), "offset": "0x0", "sha256": sha,
                                                                "map": {"flashed": "F1"}}, name="s.pcbv.yaml"))


@pytest.mark.parametrize("bad", ["abc", "g" * 64, "a" * 63, 12345, ["a" * 64]])
def test_malformed_sha256_param_is_a_fail_with_a_message(tmp_path, bad):
    res = doctor.check_images(_sha_proto(tmp_path, bad))
    assert any(c.status == FAIL and "`sha256` parameter is not a 64-character hex" in c.detail for c in res)


def test_mismatch_against_sha256_param_is_not_labelled_with_a_test_id(tmp_path):
    res = doctor.check_images(_sha_proto(tmp_path, "f" * 64))
    d = " ".join(c.detail for c in res)
    assert "the sha256 parameter" in d and "sha256 param)" in d.replace("the sha256 parameter)", "sha256 param)") and "for F1" not in d and "H1" not in d
