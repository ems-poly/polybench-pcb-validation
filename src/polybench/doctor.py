"""`polybench doctor`: read-only preflight for a bench session.

Nothing here writes to a device, transmits on CAN, switches an output or touches a process it did not start. Every line is
green (OK), amber (WARN: the batch can start, something optional is missing), red (FAIL: a thing the loaded protocol needs
is missing) or INFO (nothing wrong, nothing asked).

The checks follow the protocol. A programmer, a CAN adapter, a power analyzer or an ECAD bridge is checked only if an action
or a capability of the loaded protocol references it. With no protocol the doctor lists what is installed and raises no red
line for hardware.

  polybench doctor [--protocol FILE] [--expect-doc TEXT] [--ecad fusion|kicad] [--n6705b-host IP | --check-n6705b]
                   [--no-open] [--port 8750] [--data-dir DIR] [--json]

Optional capabilities: a hardware line whose capability the protocol declares `optional: true` is amber (WARN), never red,
for every check (package, plug-in, OpenOCD, probe, PCAN, images, DBCs, profile files). Read-only: the data folder is tested
with os.access and is never created or written.

The PCAN adapter is only *opened* (listen-only, nothing transmitted, released at once) when the protocol uses pcan.* actions,
no process has the PCBUSB driver loaded and --no-open is not given. If something holds it, the holder is named by pid and
left alone. The N6705B is queried only when --n6705b-host or --check-n6705b is given.
"""
from __future__ import annotations

import hashlib
import importlib.metadata as md
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"

ATMEL_VID, ATMEL_PID = 0x03EB, 0x2141
PEAK_VID = 0x0C72
N6705B_PORT = 5025
# Action-name prefix -> the protocol capability kind it needs. A prefix not listed has no host-side preflight (INFO).
PLUGIN_KIND = {"openocd": "programmer", "pcan": "bus_adapter", "n6705b": "dc_source", "camera": "camera", "dmm": "multimeter"}


@dataclass
class Check:
    status: str
    name: str
    detail: str = ""
    hint: str = ""
    seconds: float = 0.0


# ---------------------------------------------------------------- USB

def parse_usb_devices(text: str) -> list[dict]:
    """All devices in `ioreg -p IOUSB -l -w0` output: [{name, vid, pid, serial}]. A child's properties never leak into
    its parent because a new `+-o` line starts a new record."""
    devs: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        if "+-o " in line:
            if cur and "vid" in cur:
                devs.append(cur)
            cur = {"name": line.split("+-o ", 1)[1].split("@")[0].split("  <")[0].strip()}
            continue
        if cur is None:
            continue
        for key, rx in (("vid", r'"idVendor"\s*=\s*(\d+)'), ("pid", r'"idProduct"\s*=\s*(\d+)')):
            m = re.search(rx, line)
            if m and key not in cur:
                cur[key] = int(m.group(1))
        m = re.search(r'"USB Serial Number"\s*=\s*"([^"]*)"', line)
        if m and "serial" not in cur:
            cur["serial"] = m.group(1)
    if cur and "vid" in cur:
        devs.append(cur)
    return devs


def usb_devices() -> list[dict] | None:
    try:
        out = subprocess.run(["ioreg", "-p", "IOUSB", "-l", "-w0"], capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return None
    return parse_usb_devices(out)


# ---------------------------------------------------------------- individual checks

def check_python() -> Check:
    v = sys.version_info
    venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if v < (3, 12):
        return Check(FAIL, "Python", f"{v.major}.{v.minor} at {sys.executable}", "Polybench needs Python 3.12 or newer")
    if not venv:
        return Check(WARN, "Python", f"{v.major}.{v.minor}.{v.micro}, not in a virtual environment ({sys.executable})",
                     "run scripts/install.sh and use .venv/bin/polybench")
    return Check(OK, "Python", f"{v.major}.{v.minor}.{v.micro} in venv {sys.prefix}")


def check_packages(uses: "Uses | None" = None) -> list[Check]:
    """Packages by need. With a protocol, a package is required only if one of its actions needs it; with none, nothing is red."""
    out = []
    rows = (("python-can", "pcan", "PCAN actions"), ("cantools", "dbc", "DBC decoding"),
            ("polystage-bench", "n6705b", "N6705B actions"), ("opencv-python", "camera", "camera.snapshot only"))
    for dist, key, why in rows:
        needed = uses is not None and key in uses.groups_or_dbc
        try:
            out.append(Check(OK, f"package {dist}", md.version(dist)))
        except md.PackageNotFoundError:
            hint = f"needed for the {why}: scripts/install.sh"
            if needed:
                sev = uses.dbc_severity() if key == "dbc" else uses.severity(key)       # amber when the capability behind it is declared optional
                out.append(Check(sev, f"package {dist}", "not installed" + (" (optional capability)" if sev == WARN else ""), hint))
            else:
                out.append(Check(INFO, f"package {dist}", f"not installed ({'not used by this protocol' if uses else 'optional: ' + why})"))
    return out


def check_plugins(uses: "Uses | None" = None) -> list[Check]:
    from . import registry
    out = []
    names = registry.action_names()
    if uses is None:
        out.append(Check(INFO, "action plug-ins", f"{len(names)} installed: {', '.join(sorted(names))}"))
    else:
        bad, good, sevs = [], [], []
        for n in sorted(uses.plugins):
            if n not in names:
                bad.append(f"{n} (not registered: pip install -e .)")
                sevs.append(uses.severity(n.split(".")[0]))
                continue
            try:
                registry.find_action(n)
                good.append(n)
            except Exception as e:  # noqa: BLE001
                bad.append(f"{n} ({type(e).__name__}: {e})")
                sevs.append(uses.severity(n.split(".")[0]))
        if bad:
            out.append(Check(FAIL if FAIL in sevs else WARN, "action plug-ins", "; ".join(bad), "reinstall: scripts/install.sh"))
        else:
            out.append(Check(OK, "action plug-ins", f"the protocol's {len(good)} are found and loadable: {', '.join(good) or 'none used'}"))
    try:
        eps = [e.name for e in md.entry_points(group="polybench.ecad")]
    except Exception:
        eps = []
    out.append(Check(INFO, "ECAD bridges", ", ".join(sorted(eps)) or "none installed"))
    return out


def _vid_pid(v) -> tuple[int, int] | None:
    """[vid, pid] from a protocol (ints or '0x..' strings) -> (vid, pid), or None if absent or malformed."""
    try:
        t = tuple(int(str(x), 0) for x in v)
    except (TypeError, ValueError):
        return None
    return t if len(t) == 2 else None


class Uses:
    """What a loaded protocol references: action plug-ins, their parameters, and capability kinds (name -> optional)."""

    def __init__(self, proto=None):
        self.actions: list[tuple[str, dict]] = []      # (plugin, params)
        self.caps: dict[str, bool] = {}                # kind -> optional
        if proto is None:
            return
        for st in proto.data["steps"]:
            for sec in st.get("sections", []) or []:
                for a in sec.get("actions", []) or []:
                    if a.get("plugin"):
                        self.actions.append((str(a["plugin"]), a.get("params") or {}))
        for c in proto.data.get("capabilities") or []:
            kind = str(c.get("kind") or c.get("name"))
            self.caps[kind] = self.caps.get(kind, True) and bool(c.get("optional"))

    @property
    def plugins(self) -> set[str]:
        return {n for n, _ in self.actions}

    def prefixes(self) -> set[str]:
        return {n.split(".")[0] for n in self.plugins}

    @property
    def groups_or_dbc(self) -> set[str]:
        g = self.prefixes()
        if any(pr.get("dbc") for _, pr in self.actions):
            g.add("dbc")
        return g

    def severity(self, prefix: str) -> str:
        """FAIL for a hardware the protocol needs; WARN if its capability is declared optional."""
        kind = PLUGIN_KIND.get(prefix)
        return WARN if kind and self.caps.get(kind) is True else FAIL

    def dbc_severity(self) -> str:
        """cantools is needed by the actions that name a `dbc`: amber only if every one of them belongs to an optional capability."""
        sevs = [self.severity(n.split(".")[0]) for n, pr in self.actions if pr.get("dbc")]
        return WARN if sevs and FAIL not in sevs else FAIL

    def probes(self) -> list[tuple[str, str | None, tuple[int, int] | None]]:
        """Distinct (probe preset, probe_serial, vid_pid) of the openocd.* actions; vid_pid is None unless the action gives one."""
        seen: dict[tuple, None] = {}
        for n, pr in self.actions:
            if n.startswith("openocd."):
                seen[(str(pr.get("probe") or "atmel-ice").lower(), pr.get("probe_serial"), _vid_pid(pr.get("vid_pid")))] = None
        return list(seen)

    @property
    def has_ecad(self) -> bool:
        return "ecad" in self.caps


def check_openocd(probes: list[str] | None = None, severity: str = FAIL) -> Check:
    from .plugins.openocd import find_openocd, PROBES, VID_PID_PROBES
    from .plugins._util import PluginError
    try:
        exe = find_openocd({})
    except PluginError as e:
        return Check(severity, "OpenOCD", str(e), "brew install open-ocd")
    try:
        p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=8)
        first = ((p.stderr or "") + (p.stdout or "")).strip().splitlines()[0]
    except Exception as e:  # noqa: BLE001
        return Check(severity, "OpenOCD", f"{exe} did not run: {e}")
    m = re.search(r"(\d+)\.(\d+)", first)
    if m and (int(m.group(1)), int(m.group(2))) < (0, 12):
        return Check(WARN, "OpenOCD", f"{first} at {exe}", "tested with 0.12; older versions may lack the cmsis_dap_vid_pid command")
    names = ", ".join(PROBES[x]["name"] for x in (probes or []) if x in PROBES)
    return Check(OK, "OpenOCD", f"{first} at {exe}" + (f"; probes used: {names}" if names else ""))


def check_probe(devs: list[dict] | None, probe: str = "atmel-ice", severity: str = FAIL, serial: str | None = None,
                vid_pid: tuple[int, int] | None = None) -> Check:
    """The debug probe a protocol's openocd.* actions name. `severity` is what 'not on USB' counts as (FAIL when the
    programmer capability is required, WARN when it is optional). `vid_pid` is the action's USB restriction; when given
    it, not the preset's default, is what must be on USB (an EDBG on the Atmel-ICE preset, a generic probe)."""
    from .plugins.openocd import PROBES
    spec = PROBES[probe]
    name = spec["name"].split(" (")[0]
    label = f"probe {name}"
    vids = frozenset({vid_pid[0]}) if vid_pid else spec["vids"]
    if vids is None:
        return Check(INFO, label, f"{spec['name']}: no USB id is checked here for a generic probe; OpenOCD reports it when an action runs",
                     spec["label"])
    if devs is None:
        return Check(WARN, label, "could not list USB devices (ioreg)")
    want_pid = vid_pid[1] if vid_pid else (ATMEL_PID if probe == "atmel-ice" else None)
    found = [d for d in devs if d.get("vid") in vids]
    if not found:
        ids = ", ".join(f"{v:04x}" for v in sorted(vids))
        hint = "plug the probe in by USB"
        if probe == "atmel-ice":
            hint += "; on the 10-pin cable use the SAM port"
        return Check(severity, label, f"not enumerated (USB vendor {ids})", hint)
    desc = ", ".join(f"{d.get('name', '?')} serial {d.get('serial') or '?'} ({d.get('vid', 0):04x}:{d.get('pid', 0):04x})" for d in found)
    if want_pid is not None:
        matching = [d for d in found if d.get("pid") == want_pid]
        if not matching:
            return Check(severity if vid_pid else WARN, label, f"{desc}; no probe with product id {want_pid:04x}",
                         f"expected product id {want_pid:04x}; the OpenOCD filter will not match it")
        found = matching
        desc = ", ".join(f"{d.get('name', '?')} serial {d.get('serial') or '?'} ({d.get('vid', 0):04x}:{d.get('pid', 0):04x})" for d in found)
    if serial and not any((d.get("serial") or "") == serial for d in found):
        return Check(WARN, label, f"{desc}; none has the serial {serial} the protocol names", "check probe_serial in the action")
    if len(found) > 1 and not serial:
        return Check(WARN, label, f"{len(found)} probes: {desc}", "give probe_serial in the actions, or unplug the extra probe")
    return Check(OK, label, desc if probe == "atmel-ice" else f"{desc} [{spec['label']}]")


def check_atmel_ice(devs: list[dict] | None) -> Check:
    return check_probe(devs, "atmel-ice")


def pcan_holders() -> list[tuple[str, str]]:
    """[(pid, description)] of processes with the PCBUSB driver loaded. Never kills anything."""
    import glob
    libs = sorted({os.path.realpath(x) for x in glob.glob("/usr/local/lib/libPCBUSB*.dylib")})
    if not libs:
        return []
    try:
        out = subprocess.run(["lsof", "-F", "pc", *libs], capture_output=True, text=True, timeout=4).stdout
    except Exception:
        return []
    found: dict[str, str] = {}
    pid = None
    for ln in out.splitlines():
        if ln.startswith("p"):
            pid = ln[1:]
        elif ln.startswith("c") and pid and pid != str(os.getpid()):
            try:
                cmd = subprocess.run(["ps", "-ww", "-o", "command=", "-p", pid], capture_output=True, text=True, timeout=2).stdout.strip()
            except Exception:
                cmd = ln[1:]
            toks = cmd.split()
            py = next((t for t in toks if t.endswith(".py")), None)
            found[pid] = (os.path.basename(py) + " " + " ".join(toks[toks.index(py) + 1:][:3])).strip() if py else cmd[:80]
    return sorted(found.items())


def check_pcan(devs: list[dict] | None, open_test: bool = True, channel: str = "PCAN_USBBUS1", severity: str = FAIL, bitrate: int = 1_000_000) -> Check:
    peak = [d for d in (devs or []) if d.get("vid") == PEAK_VID]
    if devs is not None and not peak:
        return Check(severity, "PCAN adapter", "not enumerated on USB (PEAK 0c72)", "plug in the PCAN-USB")
    holders = pcan_holders()
    present = "on USB" if peak else "USB list unavailable"
    if holders:
        who = "; ".join(f"pid {p} ({c})" for p, c in holders)
        return Check(severity, "PCAN adapter", f"{present}, but not free: {who}",
                     "the program above has the PCAN driver loaded and probably holds the channel. Polybench never stops it: "
                     "close it yourself (or let its owner stop it) and run doctor again")
    if not open_test:
        return Check(OK, "PCAN adapter", f"{present}; no process has the driver loaded (not opened: --no-open)")
    try:
        import can
        logging.getLogger("can").setLevel(logging.CRITICAL)
        bus = can.Bus(interface="pcan", channel=channel, bitrate=bitrate, state=can.BusState.PASSIVE)
        time.sleep(0.1)
        try:
            bstate = str(getattr(bus.state, "name", bus.state))
        except Exception:  # noqa: BLE001
            bstate = ""
        bus.shutdown()
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        return Check(severity, "PCAN adapter", f"{present}; opening {channel} in listen-only mode failed ({msg[:160]})",
                     "another program may hold it (lsof found none with the driver loaded), or the PCBUSB driver is missing")
    return Check(OK, "PCAN adapter", f"{present}; {channel} is free (opened listen-only at {bitrate / 1e6:g} Mbit/s"
                 + (f", bus state {bstate}" if bstate else "") + ", nothing transmitted, released)",
                 "check the bus termination: 120 ohm at each end of the bus")


def check_meter(needed: bool = False, probe: bool = True, severity: str = FAIL) -> list[Check]:
    """The multimeter: hidapi present, the CH9329 cable on USB (1A86:E429), and, if probe is true, the model string from one Get Name.
    Sends only Get Name (a read). Absent is not an error unless the protocol asks for it (needed)."""
    from .meter import transport as T
    from .meter.link import MeterLink
    bad = severity if needed else INFO
    ok, why = T.hid_available()
    if not ok:
        return [Check(WARN if needed and severity == WARN else bad, "multimeter", "hidapi is not installed", "pip install -e '.[dmm]' (live meter and dmm.read)")]
    try:
        cables = T.find_cable()
    except T.TransportError as e:
        return [Check(WARN, "multimeter", str(e))]
    if not cables:
        return [Check(bad, "multimeter", "no CH9329 cable on USB (1A86:E429); the UNI-T UT161E is not connected",
                      "plug in the cable" if needed else "optional: only the live meter and dmm.read use it")]
    cab = cables[0].get("product") or "CH9329 cable"
    if not probe:
        return [Check(INFO, "multimeter", f"cable found: {cab}; not opened (--no-open)")]
    tr = T.HidTransport()
    try:
        tr.open()
    except T.TransportError as e:
        return [Check(WARN, "multimeter", f"cable found ({cab}) but cannot be opened: {e}", "close the program that holds it (the Polybench server's live meter?)")]
    try:
        name = MeterLink(tr).get_name()
    except T.TransportError as e:
        return [Check(WARN, "multimeter", f"cable found ({cab}); read failed: {e}")]
    finally:
        tr.close()
    if not name:
        return [Check(WARN, "multimeter", f"cable found ({cab}); the meter did not answer Get Name",
                      "is the meter on and awake, and is its USB function enabled?")]
    return [Check(OK, "multimeter", f"{name} on {cab}")]


def check_fusion(expect: str | None) -> Check:
    try:
        from .plugins.fusion import FusionBridge
        st = FusionBridge(simulate=False).status()
    except Exception as e:  # noqa: BLE001
        return Check(WARN, "Fusion MCP", f"{type(e).__name__}: {e}")
    if not st.get("connected"):
        return Check(WARN, "Fusion MCP", f"not reachable: {st.get('detail')}",
                     "start Fusion and open the board; highlights are simulated (manual entry still works) until then")
    doc, ed = st.get("document"), st.get("editor")
    if not doc:
        return Check(WARN, "Fusion MCP", "reachable, but no document is open", "open the board document")
    desc = f"document '{doc}', {ed} editor, {st.get('detail')}"
    if expect and expect.lower() not in str(doc).lower():
        return Check(WARN, "Fusion MCP", f"{desc}; this is not a '{expect}' document",
                     f"open the board in Fusion, or highlights will say designators are missing")
    return Check(OK, "Fusion MCP", desc)


def check_kicad(expect: str | None, link=None) -> Check:
    """KiCad API: installed, server on, socket present, a board open. Read only (settings file, then GetVersion / open documents /
    footprints). With the socket absent nothing is dialled. `link` is for tests."""
    try:
        from .kicad.bridge import KicadBridge
        st = KicadBridge(link=link, simulate=False).status() if link is not None else KicadBridge(simulate=False).status()
    except Exception as e:  # noqa: BLE001
        return Check(WARN, "KiCad API", f"{type(e).__name__}: {e}")
    if not st.get("connected"):
        fix = {"no_kipy": "pip install -e .[kicad]",
               "api_off": "enable the API server in KiCad (Preferences, Plugins) and restart KiCad, then run --ecad kicad",
               "not_running": "start KiCad and open the board; the API server must be on (Preferences, Plugins)",
               "stale_socket": "restart KiCad (its API socket file is left over)",
               "timeout": "KiCad did not answer in time: close any dialog in the PCB editor",
               "busy": "KiCad is busy: finish the tool or dialog open in the PCB editor"}.get(st.get("state"), "")
        return Check(WARN, "KiCad API", f"not usable: {st.get('detail')}", fix)
    doc = st.get("document")
    if not doc:
        return Check(WARN, "KiCad API", f"reachable (KiCad {st.get('version')}), but no board is open", "open the board in the PCB editor")
    desc = f"KiCad {st.get('version')}, board '{doc}', {st.get('detail')}"
    if expect and expect.lower() not in str(doc).lower():
        return Check(WARN, "KiCad API", f"{desc}; this is not a '{expect}' document", "open the board in KiCad")
    return Check(OK, "KiCad API", desc)


def check_n6705b(host: str | None, timeout: float = 3.0) -> Check:
    """One *IDN? on the SCPI socket. Read only: no output, no setting changes. One attempt, and only when asked for.
    The host is the argument, else POLYBENCH_N6705B_HOST, else the polystage-bench instruments.toml; there is no built-in default."""
    host = host or os.environ.get("POLYBENCH_N6705B_HOST") or _configured_host()
    if not host:
        return Check(WARN, "N6705B power analyzer", "no host known, so nothing was queried",
                     "give --n6705b-host IP, set POLYBENCH_N6705B_HOST or add the instrument to ~/.config/polystage-bench/instruments.toml")
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, N6705B_PORT), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(b"*IDN?\n")
            buf = b""
            while not buf.endswith(b"\n") and len(buf) < 512:
                chunk = s.recv(256)
                if not chunk:
                    break
                buf += chunk
        idn = buf.decode(errors="replace").strip()
    except OSError as e:
        return Check(WARN, "N6705B power analyzer", f"{host}:{N6705B_PORT} did not answer ({type(e).__name__}: {e})",
                     "check that it is on and on the network; a bench supply plus a meter work instead (typed values)")
    if "N6705" not in idn:
        return Check(WARN, "N6705B power analyzer", f"{host} answered something else: {idn[:80]!r}")
    return Check(OK, "N6705B power analyzer", f"{host}: {idn}; outputs untouched ({time.monotonic() - t0:.1f} s)")


def _configured_host() -> str | None:
    try:
        import tomllib
        p = Path.home() / ".config" / "polystage-bench" / "instruments.toml"
        d = tomllib.loads(p.read_text())
        for inst in d.get("instruments", {}).values():
            if inst.get("model") == "n6705b" and inst.get("host"):
                return inst["host"]
    except Exception:
        pass
    return None


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_manifest(path: Path) -> dict[str, str]:
    """`<sha256>  <relative path>` lines (sha256sum format) -> {basename: sha256}."""
    out: dict[str, str] = {}
    for ln in path.read_text().splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", ln.strip())
        if m:
            out[Path(m.group(2)).name] = m.group(1).lower()
    return out


def protocol_images(proto) -> list[dict]:
    """Every action whose params name a firmware `image` (openocd.flash, esptool.flash, any plug-in that uses that param):
    {action, plugin, image, test_id (its image_sha256 test), expected (that test's hard `equals`, else the action's own
    `sha256` param)}."""
    found = []
    for st in proto.data["steps"]:
        for sec in st.get("sections", []) or []:
            for a in sec.get("actions", []) or []:
                pr = a.get("params") or {}
                if a.get("plugin") and isinstance(pr.get("image"), (str, Path)) and pr.get("image"):
                    tid = (pr.get("map") or {}).get("image_sha256")
                    expected = None
                    if tid:
                        try:
                            expected = (proto.test(tid)[2].get("limit") or {}).get("equals")
                        except Exception:
                            expected = None
                    bad_sha = None
                    if "sha256" in pr and pr["sha256"] is not None:
                        v = pr["sha256"]
                        if isinstance(v, str) and re.fullmatch(r"[0-9a-fA-F]{64}", v):
                            expected = expected or v
                        else:
                            bad_sha = (f"the `sha256` parameter is not a 64-character hex string (got {str(v)[:40]!r}"
                                       f"{'' if isinstance(v, str) else ', type ' + type(v).__name__})")
                    found.append({"action": a["id"], "plugin": str(a["plugin"]), "image": str(pr["image"]), "test": tid or "sha256 param",
                                  "expected": expected, "bad_sha": bad_sha})
    return found


def check_images(proto, uses: "Uses | None" = None) -> list[Check]:
    imgs = protocol_images(proto)
    if not imgs:
        return [Check(OK, "firmware images", "the protocol flashes nothing")]
    missing, bad, manifest_miss, n_ok = [], [], [], 0
    sev_missing, sev_bad = [], []

    def sev_of(it):
        return uses.severity(it["plugin"].split(".")[0]) if uses else FAIL
    manifests: dict[Path, dict[str, str]] = {}
    for it in imgs:
        p = Path(it["image"]).expanduser()
        if it.get("bad_sha"):
            bad.append(f"{p.name}: {it['bad_sha']}")
            sev_bad.append(FAIL)
        if not p.is_file():
            missing.append(f"{p.name} ({it['action']})")
            sev_missing.append(sev_of(it))
            continue
        actual = sha256(p)
        problems = []
        if it["expected"] and actual != str(it["expected"]).lower():
            problems.append(f"differs from the protocol's expected hash ({'the sha256 parameter' if it['test'] == 'sha256 param' else 'test ' + str(it['test'])})")
        man = p.parent / "MANIFEST.txt"
        if man.is_file():
            if man not in manifests:
                manifests[man] = read_manifest(man)
            want = manifests[man].get(p.name)
            if want is None:
                manifest_miss.append(p.name)
            elif want != actual:
                problems.append("differs from MANIFEST.txt")
        else:
            manifest_miss.append(p.name)
        if problems:
            bad.append(f"{p.name}: {' and '.join(problems)}")
            sev_bad.append(sev_of(it))
        else:
            n_ok += 1
    out = []
    if missing:
        out.append(Check(FAIL if FAIL in sev_missing else WARN, "firmware images", f"missing: {', '.join(missing)}", "build or copy the firmware images named in the protocol"))
    if bad:
        out.append(Check(FAIL if FAIL in sev_bad else WARN, "firmware image hashes", "; ".join(bad),
                         "rebuild, or update the expected hashes in the protocol after a deliberate rebuild"))
    if not missing and not bad:
        extra = f" ({len(manifest_miss)} not listed in a MANIFEST.txt)" if manifest_miss else ""
        mans = ", ".join(str(m) for m in manifests) or "no MANIFEST.txt next to the images"
        out.append(Check(WARN if manifest_miss else OK, "firmware images",
                         f"{n_ok} images present; hashes match the protocol and {mans}{extra}"))
    return out


def check_param_files(proto, uses: "Uses | None" = None) -> Check:
    """Files named in action params that must exist on this Mac: N6705B profile TOMLs and OpenOCD config files."""
    missing, sevs, n = [], [], 0
    for st in proto.data["steps"]:
        for sec in st.get("sections", []) or []:
            for a in sec.get("actions", []) or []:
                pr = a.get("params") or {}
                for key in ("profile", "config"):
                    v = pr.get(key)
                    if v and (("/" in str(v)) or str(v).endswith((".toml", ".cfg"))):
                        n += 1
                        if not Path(str(v)).expanduser().is_file():
                            missing.append(f"{Path(str(v)).name} ({a['id']})")
                            sevs.append(uses.severity(str(a.get("plugin", "")).split(".")[0]) if uses else FAIL)
    if missing:
        return Check(FAIL if FAIL in sevs else WARN, "profile / config files", "missing: " + ", ".join(dict.fromkeys(missing)))
    return Check(OK, "profile / config files", f"{n} referenced, all present" if n else "none referenced")


def check_dbcs(proto, uses: "Uses | None" = None) -> Check:
    from .plugins.can_common import load_dbc_specs
    from .plugins._util import PluginError
    seen: dict[str, set[str]] = {}
    sev: dict[str, list[str]] = {}
    for st in proto.data["steps"]:
        for sec in st.get("sections", []) or []:
            for a in sec.get("actions", []) or []:
                pr = a.get("params") or {}
                if pr.get("dbc"):
                    names = list(pr.get("signals") or [])
                    if pr.get("signal"):
                        names.append(pr["signal"])
                    if (pr.get("counter") or {}).get("signal"):
                        names.append(pr["counter"]["signal"])
                    seen.setdefault(str(pr["dbc"]), set()).update(names)
                    sev.setdefault(str(pr["dbc"]), []).append(uses.severity(str(a.get("plugin", "")).split(".")[0]) if uses else FAIL)
    if not seen:
        return Check(OK, "DBC files", "the protocol uses none")
    problems, psev = [], []
    for path, names in seen.items():
        try:
            load_dbc_specs(path, sorted(names))
        except PluginError as e:
            problems.append(f"{Path(path).name}: {e}")
            psev += sev[path]
    if problems:
        return Check(FAIL if FAIL in psev else WARN, "DBC files", "; ".join(problems))
    return Check(OK, "DBC files", f"{len(seen)} files load and contain every signal the actions name")


def check_protocol(path: str):
    from .protocol import load
    try:
        proto = load(path)
    except Exception as e:  # noqa: BLE001
        return None, Check(FAIL, "protocol", f"{path}: {type(e).__name__}: {e}")
    n_t = sum(1 for _ in proto.tests())
    if proto.errors:
        return proto, Check(FAIL, "protocol lint", f"{len(proto.errors)} errors, e.g. {proto.errors[0]}", f"polybench lint {path}")
    detail = f"{proto.path.name}: {len(proto.data['steps'])} steps, {n_t} tests, 0 errors, {len(proto.warnings)} warnings, hash {proto.hash}"
    return proto, Check(WARN if proto.warnings else OK, "protocol lint", detail,
                        f"first warning: {proto.warnings[0]}" if proto.warnings else "")


def check_data_dir(data_dir: str | Path | None = None) -> Check:
    """Read-only: nothing is created and nothing is written. A folder that does not exist yet is INFO (the server makes it
    on the first batch); one that exists is tested with os.access."""
    from .store import default_data_dir
    d = Path(data_dir).expanduser() if data_dir else Path(default_data_dir())
    if not d.exists():
        return Check(INFO, "data folder", f"{d} does not exist yet; Polybench creates it when the first batch starts",
                     "use --data-dir or POLYBENCH_DATA to check another folder")
    if not d.is_dir():
        return Check(FAIL, "data folder", f"{d} exists but is not a folder")
    if not os.access(d, os.W_OK | os.X_OK):
        return Check(FAIL, "data folder", f"{d} is not writable by this user", "fix the folder's permissions or choose another with POLYBENCH_DATA")
    try:
        free = shutil.disk_usage(d).free / 1e9
    except OSError as e:
        return Check(WARN, "data folder", f"{d} is writable, free space unknown ({e})")
    return Check(OK if free > 1 else WARN, "data folder", f"{d} writable (permissions only; nothing written), {free:.0f} GB free")


def check_port(port: int) -> Check:
    with socket.socket() as s:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return Check(WARN, f"port {port}", "something is already listening", "an earlier Polybench? use --port N or close it")
    return Check(OK, f"port {port}", "free")


# ---------------------------------------------------------------- driver

def _installed_listing(devs: list[dict] | None) -> list[Check]:
    """No protocol loaded: what is installed, never red."""
    out = []
    from .plugins.openocd import find_openocd, PROBES
    from .plugins._util import PluginError
    try:
        out.append(Check(INFO, "OpenOCD", f"installed at {find_openocd({})}"))
    except PluginError:
        out.append(Check(INFO, "OpenOCD", "not installed", "only needed by protocols that program a target: brew install open-ocd"))
    known = [f"{PROBES[k]['name']} ({', '.join(f'{v:04x}' for v in sorted(PROBES[k]['vids']))})" for k in PROBES if PROBES[k]["vids"]]
    seen = []
    for d in devs or []:
        for k, spec in PROBES.items():
            if spec["vids"] and d.get("vid") in spec["vids"]:
                seen.append(f"{d.get('name', '?')} serial {d.get('serial') or '?'}")
    out.append(Check(INFO, "debug probes on USB", ", ".join(seen) if seen else "none seen", f"recognised: {'; '.join(known)}; CMSIS-DAP generic is not listed"))
    peak = [d for d in (devs or []) if d.get("vid") == PEAK_VID]
    out.append(Check(INFO, "CAN adapter on USB", "a PEAK adapter is enumerated" if peak else "none seen"))
    out.append(Check(INFO, "N6705B / ECAD", "not queried: no protocol is loaded"))
    return out


def run_checks(protocol: str | None = None, expect_doc: str | None = None, n6705b_host: str | None = None,
               open_pcan: bool = True, port: int = 8750, query_n6705b: bool = False, ecad: str | None = None,
               data_dir: str | None = None) -> list[Check]:
    """Checks follow the protocol. The N6705B is queried only if n6705b_host is given or query_n6705b is True."""
    results: list[Check] = []

    def add(item):
        items = item if isinstance(item, list) else [item]
        results.extend(items)

    def timed(fn, *a, **k):
        t0 = time.monotonic()
        try:
            r = fn(*a, **k)
        except Exception as e:  # noqa: BLE001  a broken check must not hide the others
            r = Check(FAIL, fn.__name__, f"check crashed: {type(e).__name__}: {e}")
        dt = time.monotonic() - t0
        for c in (r if isinstance(r, list) else [r]):
            c.seconds = dt
        return r

    proto, uses = None, None
    pcheck = None
    if protocol:
        t0 = time.monotonic()
        proto, pcheck = check_protocol(protocol)
        pcheck.seconds = time.monotonic() - t0
        if proto is not None:
            uses = Uses(proto)
    pre = uses.prefixes() if uses else set()
    devs = usb_devices() if (uses is None or pre & {"openocd", "pcan"}) else None

    add(timed(check_python))
    add(timed(check_packages, uses))
    add(timed(check_plugins, uses))
    if uses is None:
        add(timed(_installed_listing, usb_devices()))
    else:
        if "openocd" in pre:
            add(timed(check_openocd, [k for k, _, _ in uses.probes()], uses.severity("openocd")))
            for pname, serial, vid_pid in uses.probes():
                from .plugins.openocd import PROBES
                if pname not in PROBES:
                    add(Check(FAIL, f"probe {pname}", "not a known probe preset", f"use one of: {', '.join(PROBES)}"))
                else:
                    add(timed(check_probe, devs, pname, uses.severity("openocd"), serial, vid_pid))
        if "pcan" in pre:
            add(timed(check_pcan, devs, open_pcan, severity=uses.severity("pcan")))
        if "n6705b" in pre:
            if n6705b_host or query_n6705b:
                add(timed(check_n6705b, n6705b_host))
            else:
                add(Check(INFO, "N6705B power analyzer", "used by the protocol; not queried (opt-in)",
                          "add --n6705b-host IP or --check-n6705b to send one *IDN? (read only)"))
        if uses.has_ecad or ecad:
            which = (ecad or os.environ.get("POLYBENCH_ECAD") or "fusion").lower()
            add(timed(check_kicad if which == "kicad" else check_fusion, expect_doc))
        else:
            add(Check(INFO, "ECAD bridge", "the protocol declares no ecad capability; not checked"))
        for pf in sorted(pre - set(PLUGIN_KIND) - {"aoi", "demo"}):
            add(Check(INFO, f"{pf}.* actions", "no host-side preflight for this plug-in yet"))
    add(timed(check_meter, bool(uses and "dmm" in pre), open_pcan, uses.severity("dmm") if uses and "dmm" in pre else FAIL))
    add(timed(check_data_dir, data_dir))
    add(timed(check_port, port))
    if pcheck is not None:
        add(pcheck)
        if proto is not None:
            add(timed(check_images, proto, uses))
            add(timed(check_dbcs, proto, uses))
            add(timed(check_param_files, proto, uses))
    else:
        add(Check(INFO, "protocol", "not given", "add --protocol <file> to check only what that protocol uses, plus lint, images and DBCs"))
    return results


def render(results: list[Check], color: bool) -> str:
    tag = {OK: ("OK  ", "32"), WARN: ("WARN", "33"), FAIL: ("FAIL", "31"), INFO: ("INFO", "36")}
    lines = []
    for c in results:
        t, col = tag[c.status]
        label = f"\033[{col};1m[{t}]\033[0m" if color else f"[{t}]"
        lines.append(f"{label} {c.name:<24} {c.detail}")
        if c.hint and c.status != OK:
            lines.append(f"       {'':<24} -> {c.hint}")
        elif c.hint and c.status == OK and "termination" in c.hint:
            lines.append(f"       {'':<24} -> {c.hint}")
    n = {s: sum(1 for c in results if c.status == s) for s in (OK, WARN, FAIL, INFO)}
    verdict = ("NOT READY: fix the red lines" if n[FAIL] else
               "READY with warnings: the amber lines are optional or manual-entry items" if n[WARN] else "READY")
    lines.append("")
    lines.append(f"doctor: {n[OK]} ok, {n[INFO]} info, {n[WARN]} warn, {n[FAIL]} fail. {verdict}. (read-only: nothing was written to a device)")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="polybench doctor", description="read-only preflight; checks only what the protocol uses")
    ap.add_argument("--protocol", help="protocol file: its lint, programmers, adapters, instruments, images, hashes and DBCs are checked")
    ap.add_argument("--expect-doc", default=None, help="warn if the open ECAD document name lacks this text (no default)")
    ap.add_argument("--ecad", choices=("fusion", "kicad"), help="ECAD bridge to check (default: POLYBENCH_ECAD, else fusion)")
    ap.add_argument("--n6705b-host", help="query the N6705B at this IP (one read-only *IDN?). Without this flag or --check-n6705b it is never queried")
    ap.add_argument("--check-n6705b", action="store_true", help="query the N6705B at the host from POLYBENCH_N6705B_HOST or instruments.toml")
    ap.add_argument("--no-open", action="store_true", help="do not open the PCAN adapter (listen-only) to prove it is free")
    ap.add_argument("--port", type=int, default=8750)
    ap.add_argument("--data-dir", help="data folder to check (default: POLYBENCH_DATA, else ~/polybench-data). Never created or written")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res = run_checks(a.protocol, a.expect_doc or None, a.n6705b_host, not a.no_open, a.port, a.check_n6705b, a.ecad, a.data_dir)
    if a.json:
        print(json.dumps([asdict(c) for c in res], indent=1))
    else:
        print(render(res, sys.stdout.isatty()))
    return 1 if any(c.status == FAIL for c in res) else 0
