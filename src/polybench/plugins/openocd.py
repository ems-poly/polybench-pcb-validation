"""Actions `openocd.identify` and `openocd.flash`: a debug probe and a target over SWD, driven by OpenOCD.

The probe and the target are protocol parameters (see PROBES and TARGETS). The defaults are the Atmel-ICE (CMSIS-DAP) and
the SAM D/L/C21 target script, which is the combination that has been run (probe-absent path only; see
docs/plugins/OPENOCD_VERIFICATION.md). With the defaults the command line is

    openocd -f interface/cmsis-dap.cfg -c "cmsis_dap_vid_pid 0x03eb 0x2141" -f target/at91samdXX.cfg ... -c "program <image> verify reset"

Parameters (all optional; `docs/plugins/openocd.md` has the full table):

    probe         atmel-ice (default) | cmsis-dap | stlink | jlink
    target        sam (default; aliases samd, saml, samc, samd21, samc21)
    target_cfg    an OpenOCD target script that replaces the preset's (flash works; the id read does not, "not supported yet")
    interface     an OpenOCD interface script that replaces the probe preset's
    vid_pid       [vid, pid] USB restriction (atmel-ice and cmsis-dap only)
    probe_serial  adapter serial number
    config        one OpenOCD config file that replaces interface and target altogether

Each run starts its own OpenOCD child with a hard timeout owned here, so a hung probe never hangs the bench. Output is
classified into operator errors:

  no probe          OpenOCD finds no matching adapter (cable, USB, another program holding the probe).
  target silent     SWD answers nothing: target unpowered or SWD wires (CLK, DIO, GND, RST) not making contact. OpenOCD does
                    not report target voltage on every probe, so these two cannot be told apart here.
  device locked     the security bit is set: chip-erase is needed.

The action never decides pass or fail of a test. It proposes values; the operator accepts them. It starts and stops only
its own OpenOCD child; it never touches an OpenOCD it did not start.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from ._util import ParamError, PluginError, need, safe_name, sha256_file, stamp, to_float, to_int

OPENOCD_CANDIDATES = ("/opt/homebrew/bin/openocd", "/usr/local/bin/openocd")

# Probe presets. `vid_pid` is the default USB restriction (None: no restriction command is issued); `vids` are the USB
# vendor ids the doctor and the multi-probe check look for (None: not listed here, OpenOCD decides). `label` is the
# readiness record, kept next to the code so the docs and the doctor read the same text.
PROBES: dict[str, dict] = {
    "atmel-ice": {"name": "Atmel-ICE (CMSIS-DAP)", "interface": "interface/cmsis-dap.cfg", "driver": "cmsis-dap",
                  "vid_pid": (0x03EB, 0x2141), "vids": frozenset({0x03EB}), "pre": [],
                  "label": "HARDWARE-VALIDATED for the probe-absent path only; identify and flash on a target are UNVALIDATED"},
    "cmsis-dap": {"name": "CMSIS-DAP probe", "interface": "interface/cmsis-dap.cfg", "driver": "cmsis-dap",
                  "vid_pid": None, "vids": None, "pre": [], "label": "UNVALIDATED (command line tested on the host only)"},
    "stlink": {"name": "ST-LINK", "interface": "interface/stlink.cfg", "driver": "stlink",
               "vid_pid": None, "vids": frozenset({0x0483}), "pre": ["transport select hla_swd"], "label": "UNVALIDATED (command line tested on the host only)"},
    "jlink": {"name": "SEGGER J-Link", "interface": "interface/jlink.cfg", "driver": "jlink",
              "vid_pid": None, "vids": frozenset({0x1366}), "pre": ["transport select swd"],
              "label": "UNVALIDATED (command line tested on the host only)"},
}
DEFAULT_PROBE = "atmel-ice"
VID_PID_PROBES = ("atmel-ice", "cmsis-dap")        # the only drivers whose VID:PID command is implemented here
# Backwards-compatible names.
INTERFACE_CFG = PROBES[DEFAULT_PROBE]["interface"]
ATMEL_ICE_VID, ATMEL_ICE_PID = PROBES[DEFAULT_PROBE]["vid_pid"]
ATMEL_ICE_PIDS = {0x2141: "Atmel-ICE (CMSIS-DAP)"}

# SAM C20/C21 DS60001479D: 9.3 NVM User Row (Table 9-4) is read at 0x00804000; 9.6 Serial Number = four 32-bit words at
# 0x0080A00C, 0x0080A040, 0x0080A044, 0x0080A048 (uniqueness only guaranteed over all 128 bits). Raw words are always
# recorded as evidence.
NVM_USER_ROW_ADDR = 0x00804000
SERIAL_ADDRS = (0x0080A00C, 0x0080A040, 0x0080A044, 0x0080A048)
# DSU DID register: datasheet internal offset 0x18 on the DSU base 0x41002000; the debugger-visible alias adds 0x100.
# Both are read, the one that decodes as a Cortex-M0+ part (PROCESSOR nibble 0x1) is used. Not yet checked on silicon.
DID_ADDRS = (0x41002018, 0x41002118)

# Target families: the target script and the device-id read. Only the SAM family has a read; for another script the
# identify action stops with "not supported yet". Add a family by giving it `cfg` and an `identify` function set.
TARGETS: dict[str, dict] = {
    "sam": {"name": "SAM D/L/C21", "cfg": "target/at91samdXX.cfg", "identify": True,
            "label": "HOST-ONLY (flash and id read not yet run on silicon)"},
}
TARGET_ALIASES = {"samd": "sam", "saml": "sam", "samc": "sam", "samd21": "sam", "samc21": "sam", "saml21": "sam", "at91samd": "sam"}
DEFAULT_TARGET = "sam"
TARGET_CFG = TARGETS[DEFAULT_TARGET]["cfg"]
IDENTIFY_TIMEOUT_S = 15.0
FLASH_TIMEOUT_S = 90.0


# Production-setting NVM user row word 0 (Table 9-4: BOOTPROT 7, EEPROM 7, BODVDD level 8, enabled, action 1, BODCORE 0xA8)
# and a fixed made-up serial for the simulator.
SIM_READS = {NVM_USER_ROW_ADDR: 0x7 | 1 << 3 | 7 << 4 | 1 << 7 | 8 << 8 | 1 << 15 | 0xA8 << 17,
             SERIAL_ADDRS[0]: 0x5A31C0DE, SERIAL_ADDRS[1]: 0x0B7A1C21, SERIAL_ADDRS[2]: 0x93D04E7F, SERIAL_ADDRS[3]: 0x2F8160A5}


class ProbeError(PluginError):
    pass


# Characters that OpenOCD's Tcl would read as syntax inside a `-c` command. Values that are interpolated into a `-c` string
# (the probe serial, the image path) are refused if they contain one, whitespace or a control character: rejecting is safer
# than quoting, and no real serial number or build path needs them.
TCL_UNSAFE = frozenset(';[]$"\\{}')


def tcl_word(value, what: str, spaces: bool = False) -> str:
    """Return `value` as text if it is safe to place in an OpenOCD `-c` command, else raise ParamError naming the character.
    A space is allowed only when `spaces` is true (a path inside double quotes); a serial number is one word."""
    text = str(value)
    bad = sorted({c for c in text if c in TCL_UNSAFE or ord(c) < 32 or ord(c) == 127 or (c.isspace() and not (spaces and c == " "))})
    if bad:
        shown = " ".join(repr(c) if ord(c) < 32 or ord(c) == 127 else c for c in bad)
        raise ParamError(f"{what} contains a character that is not allowed because OpenOCD would read it as a command ({shown}). "
                         f"Value: {text[:80]!r}")
    return text


def usb_serial(serial: str) -> str:
    """A serial number read from USB, checked the same way. The operator did not type it, so the message says so."""
    try:
        return tcl_word(serial, "probe_serial")
    except ParamError as e:
        raise ParamError(f"The probe's own USB serial number is unusable: it contains a character OpenOCD would read as a command "
                         f"({str(e).split('(',1)[-1].split(')',1)[0]}). Set `probe_serial` in the action params to a safe value, "
                         f"or use a `config` file.") from None


PID_FILE = "openocd.pid"


def _proc_command(pid: int) -> str | None:
    """Command line of a live process, or None when it is gone. `ps` only reads."""
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))], capture_output=True, text=True, timeout=4).stdout.strip()
    except Exception:
        return None
    return out or None


def surviving_openocd(workdir: str | Path | None = None, data_dir: str | Path | None = None) -> list[dict]:
    """OpenOCD processes started by an earlier run of this plug-in that are still alive: [{pid, command, run, started}].
    Found through the `openocd.pid` file every run writes in its run folder: the siblings of `workdir` and every
    `<data_dir>/<batch>/runs/<run>/`. A pid file whose pid is gone, or now belongs to a process that is not an openocd, is
    ignored. Nothing is ever stopped here."""
    files: set[Path] = set()
    if workdir:
        wd = Path(workdir).resolve()
        files |= {f for f in wd.parent.glob(f"*/{PID_FILE}") if f.parent != wd}
    if data_dir is None:
        from ..store import default_data_dir
        data_dir = default_data_dir()
    files |= set(Path(data_dir).glob(f"*/runs/*/{PID_FILE}"))
    found = []
    for f in sorted(files):
        try:
            info = json.loads(f.read_text())
            pid = int(info["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        cmd = _proc_command(pid)
        if cmd and any("openocd" in Path(t).name.lower() for t in cmd.split()[:2]):
            found.append({"pid": pid, "command": cmd[:120], "run": f.parent.name, "started": info.get("started", "?")})
    return found


def orphan_message(found: list[dict]) -> str:
    return " ".join(
        f"An OpenOCD from an earlier run ({o['run']}) is still running: pid {o['pid']}, '{o['command']}'. It probably holds the probe. "
        f"Polybench did not start it in this run and will not stop it. If you are sure nothing is programming, stop it yourself: "
        f"kill {o['pid']}  (check first with: ps -p {o['pid']})." for o in found)


def find_openocd(params: dict | None = None) -> str:
    p = (params or {}).get("openocd") or os.environ.get("POLYBENCH_OPENOCD")
    if p:
        if not Path(p).exists():
            raise PluginError(f"openocd not found at {p}")
        return p
    found = shutil.which("openocd")
    if found:
        return found
    for c in OPENOCD_CANDIDATES:
        if Path(c).exists():
            return c
    raise PluginError("openocd is not installed. Install it with: brew install open-ocd")


def resolve_probe(params: dict) -> tuple[str, dict]:
    name = str(params.get("probe") or DEFAULT_PROBE).lower()
    if name not in PROBES:
        raise ParamError(f"unknown probe '{name}'; choose one of: {', '.join(PROBES)} (or give `interface` / `config`)")
    return name, PROBES[name]


def resolve_target(params: dict) -> tuple[str | None, dict | None]:
    """(family key, family entry). A custom `target_cfg` with no `target`, or with a `target` name that has no preset, has no family:
    flash works, identify does not."""
    raw = params.get("target")
    if not raw:
        return (None, None) if params.get("target_cfg") else (DEFAULT_TARGET, TARGETS[DEFAULT_TARGET])
    key = TARGET_ALIASES.get(str(raw).lower(), str(raw).lower())
    if key not in TARGETS:
        if params.get("target_cfg"):
            return None, None            # the script is the user's: flash works, the id read is "not supported yet"
        raise ParamError(f"target '{raw}' is not supported yet (presets: {', '.join(TARGETS)}). "
                         "Programming another chip works with `target_cfg: <OpenOCD target script>`; the device-id read does not.")
    return key, TARGETS[key]


def require_identify(params: dict) -> None:
    key, fam = resolve_target(params)
    if fam is None or not fam.get("identify"):
        raise ParamError("The device-id / serial-number read is not supported yet for this target "
                         f"(implemented: {', '.join(k for k, v in TARGETS.items() if v.get('identify'))}). "
                         "openocd.flash still works with `target_cfg`.")


def classify_openocd_output(text: str, probe: str = DEFAULT_PROBE, params: dict | None = None) -> str | None:
    """Map OpenOCD's output to an operator message, or None if nothing recognisable went wrong."""
    t = text.lower()
    pname = PROBES.get(probe, PROBES[DEFAULT_PROBE])["name"]
    short = pname.split(" (")[0]
    if "unable to find a matching cmsis-dap device" in t or "no cmsis-dap" in t or "could not find" in t and "cmsis" in t:
        return (f"No programmer found. Check that the {short} is plugged in by USB and that no other program "
                "(a vendor IDE, another OpenOCD) holds it.")
    if "unable to open cmsis-dap device" in t or "failed to open" in t and "cmsis" in t:
        return f"The {short} was found but cannot be opened. Another program may hold it; unplug and replug it."
    if probe in ("stlink", "jlink") and ("no st-link" in t or "open failed" in t or "no j-link" in t or "could not connect to j-link" in t
                                          or "unable to match requested speed" in t and "usb" in t):
        return (f"No programmer found. Check that the {short} is plugged in by USB and that no other program "
                "(a vendor IDE, another OpenOCD) holds it.")
    if "security bit" in t or "device is locked" in t or "protected" in t and "at91samd" in t:
        try:
            key, _fam = resolve_target(params or {})
        except ParamError:
            key = None
        if key == "sam":
            iface = (params or {}).get("interface") or PROBES.get(probe, PROBES[DEFAULT_PROBE])["interface"]
            tcfg = (params or {}).get("target_cfg") or TARGET_CFG
            return ("The device is locked (security bit set). A chip erase is needed before it can be programmed: "
                    f"openocd -f {iface} -f {tcfg} -c \"init; at91samd chip-erase; exit\". This erases the chip.")
        return ("The device is locked (read or write protection set). It must be unlocked or erased before it can be programmed; "
                "use the chip vendor's procedure for this part. Erasing deletes the firmware on it.")
    silent = ("cannot read idr", "could not initialise the debug port", "swd ack", "dap transaction",
              "failed to read memory", "dpidr", "no ack", "target not examined", "examination failed",
              "wait response", "fault response", "invalid ack", "jtag scan chain", "unexpected idcode",
              "error connecting dp")
    if any(s in t for s in silent):
        return ("Target not responding on SWD. Check target power, that the CLK, DIO and GND connections "
                "are in contact with the probe, and the RST wire. OpenOCD does not report target voltage on every probe, so "
                "an unpowered board looks the same as an open wire.")
    return None


def usb_probes(probe: str = DEFAULT_PROBE) -> list[dict]:
    """Probes of the given preset on USB: [{'serial','name','vid','pid'}]. macOS ioreg; empty list on any trouble or when the
    preset has no known USB vendor id (generic CMSIS-DAP: OpenOCD decides)."""
    vids = PROBES[probe]["vids"]
    if not vids:
        return []
    try:
        out = subprocess.run(["ioreg", "-p", "IOUSB", "-l", "-w0"], capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return []
    return parse_ioreg(out, vids)


def parse_ioreg(out: str, vids: frozenset | set | None = None) -> list[dict]:
    vids = vids or PROBES[DEFAULT_PROBE]["vids"]
    probes: list[dict] = []
    cur: dict = {}
    for line in out.splitlines():
        if "+-o " in line:
            if cur.get("vid") in vids:
                probes.append(cur)
            cur = {}
        m = re.search(r'"idVendor"\s*=\s*(\d+)', line)
        if m:
            cur["vid"] = int(m.group(1))
        m = re.search(r'"idProduct"\s*=\s*(\d+)', line)
        if m:
            cur["pid"] = int(m.group(1))
        m = re.search(r'"USB Serial Number"\s*=\s*"([^"]*)"', line)
        if m:
            cur["serial"] = m.group(1)
        m = re.search(r'"USB Product Name"\s*=\s*"([^"]*)"', line)
        if m:
            cur["name"] = m.group(1)
    if cur.get("vid") in vids:
        probes.append(cur)
    return [{"serial": p.get("serial", ""), "name": p.get("name") or ATMEL_ICE_PIDS.get(p.get("pid"), "USB probe"),
             "vid": p["vid"], "pid": p.get("pid")} for p in probes]


def parse_reads(text: str) -> dict[int, int]:
    """Every `0xADDRESS: VALUE` memory-word line OpenOCD printed (mdw output)."""
    return {int(m.group(1), 16): int(m.group(2), 16)
            for m in re.finditer(r"0x([0-9a-fA-F]{8}):\s+([0-9a-fA-F]{8})", text)}


def decode_nvm_user_row(word0: int) -> dict:
    """SAM C21 NVM User Row word 0 (DS60001479D Table 9-4): BOOTPROT[2:0], BODVDD Level[13:8], BODVDD Disable[14],
    BODVDD Action[16:15]. Production values: BOOTPROT 7, level 8, enabled (disable bit 0), action 1."""
    return {"bootprot": word0 & 0x7, "bodvdd_level": (word0 >> 8) & 0x3F, "bodvdd_disable": (word0 >> 14) & 1,
            "bodvdd_enabled": 0 if (word0 >> 14) & 1 else 1, "bodvdd_action": (word0 >> 15) & 3}


def uid_from_reads(reads: dict[int, int]) -> str | None:
    """128-bit serial number as 32 hex characters, word 0 first (same order as identify.sh)."""
    if not all(a in reads for a in SERIAL_ADDRS):
        return None
    return "".join(f"{reads[a]:08X}" for a in SERIAL_ADDRS)


def decode_did(did: int) -> dict:
    """SAM C21 DSU DID fields (datasheet 'DSU DID'): PROCESSOR[31:28] FAMILY[27:23] SERIES[21:16] DIE[15:12]
    REVISION[11:8] DEVSEL[7:0]."""
    return {"processor": (did >> 28) & 0xF, "family": (did >> 23) & 0x1F, "series": (did >> 16) & 0x3F,
            "die": (did >> 12) & 0xF, "revision": (did >> 8) & 0xF, "devsel": did & 0xFF}


def plausible_did(v: int) -> bool:
    return v not in (0, 0xFFFFFFFF) and (v >> 28) == 0x1


def parse_identify(text: str) -> dict:
    """Pull the device id and flash size out of OpenOCD's output. Missing fields are None."""
    did = None
    reads = {}
    for m in re.finditer(r"0x([0-9a-fA-F]{8}):\s+([0-9a-fA-F]{8})", text):
        reads[int(m.group(1), 16)] = int(m.group(2), 16)
    for a in DID_ADDRS:
        if a in reads and plausible_did(reads[a]):
            did = reads[a]
            break
    size = None
    m = re.search(r"at 0x0*0+,\s*size\s+0x([0-9a-fA-F]+)", text) or re.search(r"size\s+0x([0-9a-fA-F]+)", text)
    if m:
        size = int(m.group(1), 16)
    part = None
    m = re.search(r"(SAM\s?[CDL]2\d[A-Z]\d{2}[A-Z]?)", text, re.I)
    if m:
        part = m.group(1).upper().replace(" ", "")
    return {"did": did, "flash_bytes": size, "part": part, "reads": {hex(k): hex(v) for k, v in reads.items()}}


class _OcdBase:
    capabilities = ["programmer"]
    name = ""
    _cls: tuple = (DEFAULT_PROBE, None)      # (probe, params) for the failure messages; set at the start of each run

    def _begin(self, params: dict) -> None:
        pname, _ = resolve_probe(params)
        self._cls = (pname, params)

    def _args(self, params: dict, commands: list[str]) -> list[str]:
        """OpenOCD command line from the probe and target presets (see the module header for the parameters). With the default
        probe, `vid_pid` limits OpenOCD to that USB id so it never opens any other USB device (a CAN adapter included).
        `config: <path>` replaces interface and target with one OpenOCD config file, which must do its own filtering."""
        exe = find_openocd(params)
        speed = to_int(params.get("speed_khz", 1000), "speed_khz")
        pname, probe = resolve_probe(params)
        cfg = params.get("config")
        if cfg:
            cfgp = Path(str(cfg)).expanduser()
            if not cfgp.is_file():
                raise ParamError(f"OpenOCD config not found: {cfgp}")
            args = [exe, "-f", str(cfgp)]
        else:
            vid_pid = probe["vid_pid"]
            if params.get("vid_pid"):
                if pname not in VID_PID_PROBES:
                    raise ParamError(f"vid_pid is only implemented for {', '.join(VID_PID_PROBES)}; "
                                     f"for {pname} use probe_serial, or a `config` file")
                try:
                    vid_pid = tuple(to_int(x, "vid_pid") for x in params["vid_pid"])
                    if len(vid_pid) != 2:
                        raise ValueError
                except (TypeError, ValueError):
                    raise ParamError("vid_pid must be a list [vid, pid]") from None
            args = [exe, "-f", tcl_word(params.get("interface") or probe["interface"], "interface", spaces=True)]
            for c in probe["pre"]:
                args += ["-c", c]
            if vid_pid and pname in VID_PID_PROBES:
                args += ["-c", f"cmsis_dap_vid_pid 0x{vid_pid[0]:04x} 0x{vid_pid[1]:04x}"]
            tcfg = params.get("target_cfg")
            if not tcfg:
                _key, fam = resolve_target(params)
                tcfg = fam["cfg"]
            args += ["-f", tcl_word(tcfg, "target_cfg", spaces=True)]
        serial = params.get("probe_serial")
        if serial:
            args += ["-c", f"adapter serial {tcl_word(serial, 'probe_serial')}"]
        args += ["-c", f"adapter speed {speed}"]
        for c in commands:
            args += ["-c", c]
        return args

    def _run(self, args: list[str], timeout: float, ctx=None) -> tuple[int, str]:
        """Run OpenOCD with a timeout owned here. Cancelling asks it to stop (SIGTERM to our own child only). While the child
        runs, `openocd.pid` in the run folder names it, so that if this process is killed (SIGKILL) the next run and
        safe_state can report the survivor. The file is removed when the child has exited."""
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        pidfile = self._write_pidfile(ctx, p, args)
        try:
            return self._wait(p, timeout, ctx)
        finally:
            if pidfile and p.poll() is not None:
                pidfile.unlink(missing_ok=True)

    @staticmethod
    def _write_pidfile(ctx, p, args) -> Path | None:
        if ctx is None:
            return None
        f = Path(ctx.workdir) / PID_FILE
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"pid": p.pid, "name": "openocd", "started": stamp(), "args0": Path(args[0]).name,
                                     "owner_pid": os.getpid()}))
        except OSError:
            return None
        return f

    def _wait(self, p, timeout: float, ctx=None) -> tuple[int, str]:
        t0 = time.monotonic()
        out = ""
        while True:
            try:
                out, _ = p.communicate(timeout=0.5)
                return p.returncode, out
            except subprocess.TimeoutExpired:
                pass
            if ctx is not None and ctx.cancelled():
                out = self._stop(p)
                raise PluginError("cancelled")
            if time.monotonic() - t0 > timeout:
                out = self._stop(p)
                msg = classify_openocd_output(out or "", *self._cls) or "OpenOCD did not finish"
                raise PluginError(f"{msg} (timed out after {timeout:.0f} s)")

    def _run_noted(self, args, timeout, ctx, note: str):
        """_run, with the surviving-OpenOCD note appended to a failure (not to a cancel)."""
        try:
            return self._run(args, timeout, ctx)
        except PluginError as e:
            if note and str(e) != "cancelled":
                raise PluginError(str(e) + note) from None
            raise

    def _orphans(self, ctx) -> str:
        """Before a run: name any OpenOCD an earlier run left behind (a log line, and a suffix for failure messages)."""
        found = surviving_openocd(ctx.workdir)
        if not found:
            return ""
        note = orphan_message(found)
        ctx.log(note, "warn")
        return "\n" + note

    @staticmethod
    def _stop(p) -> str:
        """Stop OpenOCD, which this action started (its own process group, so a wrapper's children go too)."""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(p.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                out, _ = p.communicate(timeout=3)
                return out or ""
            except subprocess.TimeoutExpired:
                continue
        return ""

    def safe_state(self) -> str:
        """Read-only: reports an OpenOCD left behind by an earlier run (found through its pid file), never stops one."""
        found = surviving_openocd()
        if found:
            return orphan_message(found)
        return "no OpenOCD process from an earlier run is alive; the probe is released when OpenOCD exits"

    def selftest(self) -> bool:
        return self._selftest()

    def _selftest(self) -> bool:  # overridden
        return True


class OpenocdIdentify(_OcdBase):
    """params: probe (atmel-ice | cmsis-dap | stlink | jlink), target (sam), target_cfg, interface, probe_serial (optional),
    speed_khz (1000), openocd (path), config (OpenOCD config file replacing interface + target, e.g. an nRESET variant),
    vid_pid (atmel-ice default [0x03eb, 0x2141]: OpenOCD opens only that probe),
    map: {probe_serial, device_id, flash_kb, bootprot, bodvdd_level, bodvdd_enabled, nvm_user_row, uid} -> test ids,
    expect_flash_kb (optional, logged only). Proposes: probe serial, DSU DID as 0xXXXXXXXX, flash size in KiB, and (when
    mapped) NVM user row BOOTPROT / BODVDD level / enable and the 128-bit serial number as 32 hex characters."""

    name = "openocd.identify"

    def run(self, ctx) -> None:
        p = ctx.params
        mp = p.get("map") or {}
        self._begin(p)
        pname, probe = resolve_probe(p)
        require_identify(p)
        if p.get("probe_serial"):
            tcl_word(p["probe_serial"], "probe_serial")
        if ctx.simulate:
            ctx.log(f"simulated: {probe['name']} 'J41800012345', SAM C21")
            serial, did, kb, part = "J41800012345", 0x11001106, 256, "SAMC21"
            reads = dict(SIM_READS)
        else:
            note = self._orphans(ctx)
            ctx.progress(0.05, f"looking for the {probe['name']} on USB")
            probes = usb_probes(pname)
            if not probes:
                ctx.log(f"No {probe['name']} listed on USB (ioreg). Trying OpenOCD anyway for a clear message.", "warn")
            else:
                for pr in probes:
                    ctx.log(f"USB: {pr['name']} serial {pr['serial'] or '?'}")
            want = p.get("probe_serial")
            if not want and len(probes) == 1:
                want = usb_serial(probes[0]["serial"]) if probes[0]["serial"] else None
            if not want and len(probes) > 1:
                raise ProbeError(f"More than one {probe['name']} is connected; give probe_serial in the action params: "
                                 + ", ".join(pr["serial"] for pr in probes))
            q = dict(p)
            if want:
                q["probe_serial"] = want
            # each read is wrapped so one failing command (an alias the part does not decode) does not hide the rest
            cmds = ["init"] + [f"catch {{{c}}} e; echo $e" for c in
                               ["flash probe 0", "flash banks"]
                               + [f"mdw 0x{a:08x}" for a in DID_ADDRS]
                               + [f"mdw 0x{a:08x}" for a in (NVM_USER_ROW_ADDR, *SERIAL_ADDRS)]] + ["shutdown"]
            ctx.progress(0.2, "connecting over SWD")
            rc, out = self._run_noted(self._args(q, cmds), IDENTIFY_TIMEOUT_S, ctx, note)
            msg = classify_openocd_output(out, *self._cls)
            info = parse_identify(out)
            if msg and info["did"] is None:
                raise ProbeError(msg + note)
            if info["did"] is None:
                tail = "\n".join(out.strip().splitlines()[-6:])
                raise ProbeError("OpenOCD ran but no device id was read:\n" + tail + note)
            serial = want or ""
            did, part = info["did"], info["part"]
            kb = info["flash_bytes"] // 1024 if info["flash_bytes"] else None
            ctx.log(f"DID fields: {decode_did(did)}")
            reads = parse_reads(out)
        exp = p.get("expect_flash_kb")
        if exp and kb and int(exp) != kb:
            ctx.log(f"flash size {kb} KiB differs from the expected {exp} KiB", "warn")
        ctx.progress(0.9, "reporting")
        ctx.log(f"probe {serial or '?'}; DID 0x{did:08X}; part {part or '?'}; flash {kb if kb else '?'} KiB")
        self._extras(ctx, mp, reads)
        if mp.get("probe_serial") and serial:
            ctx.propose(mp["probe_serial"], serial, None, None, serial)
        if mp.get("device_id"):
            ctx.propose(mp["device_id"], f"0x{did:08X}", None, None, f"0x{did:08X}")
        if mp.get("flash_kb") and kb:
            ctx.propose(mp["flash_kb"], str(kb), "KiB", None, str(kb))
        ctx.progress(1.0, "done")

    @staticmethod
    def _extras(ctx, mp: dict, reads: dict[int, int]) -> None:
        """NVM user row (BOOTPROT, BODVDD) and the 128-bit serial number. Anything that was not read is logged, never guessed."""
        row = reads.get(NVM_USER_ROW_ADDR)
        uid = uid_from_reads(reads)
        wanted = {"bootprot", "bodvdd_level", "bodvdd_enabled", "nvm_user_row", "uid"} & set(mp)
        if not wanted:
            return
        path = Path(ctx.workdir) / f"{safe_name(ctx.board)}_identify_{stamp()}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        info = {"nvm_user_row_addr": f"0x{NVM_USER_ROW_ADDR:08X}", "serial_addrs": [f"0x{a:08X}" for a in SERIAL_ADDRS],
                "raw_words": {f"0x{a:08X}": f"{reads[a]:08X}" for a in (NVM_USER_ROW_ADDR, *SERIAL_ADDRS) if a in reads},
                "source": "DS60001479D 9.3 Table 9-4 (NVM User Row), 9.6 (Serial Number)"}
        if row is not None:
            info["nvm_user_row"] = decode_nvm_user_row(row)
        info["uid"] = uid
        path.write_text(json.dumps(info, indent=1))
        ev = str(path)
        if row is None:
            ctx.log("NVM user row was not read (no mdw output for 0x00804000); BOOTPROT and BODVDD are not proposed", "warn")
        else:
            d = decode_nvm_user_row(row)
            ctx.log(f"NVM user row word 0 = 0x{row:08X}: BOOTPROT {d['bootprot']}, BODVDD level {d['bodvdd_level']}, "
                    f"{'enabled' if d['bodvdd_enabled'] else 'DISABLED'}, action {d['bodvdd_action']}")
            if "nvm_user_row" in mp:
                ctx.propose(mp["nvm_user_row"], f"0x{row:08X}", None, ev, f"0x{row:08X}")
            if "bootprot" in mp:
                ctx.propose(mp["bootprot"], str(d["bootprot"]), None, ev, f"0x{row:08X} & 7")
            if "bodvdd_level" in mp:
                ctx.propose(mp["bodvdd_level"], str(d["bodvdd_level"]), None, ev, f"(0x{row:08X} >> 8) & 0x3F")
            if "bodvdd_enabled" in mp:
                ctx.propose(mp["bodvdd_enabled"], "Y" if d["bodvdd_enabled"] else "N", None, ev, f"bit 14 = {d['bodvdd_disable']}")
        if uid is None:
            if "uid" in mp:
                ctx.log("serial number words were not all read; the 128-bit UID is not proposed", "warn")
        else:
            ctx.log(f"128-bit serial number {uid}")
            if "uid" in mp:
                ctx.propose(mp["uid"], uid, None, ev, " ".join(f"{reads[a]:08X}" for a in SERIAL_ADDRS))

    def _selftest(self) -> bool:
        sample = ("Info : SAMC21J18A ...\n#0 : samc21.flash (at91samd) at 0x00000000, size 0x00040000, buswidth 0, chipwidth 0\n"
                  "0x41002018: 00000000\n0x41002118: 11001106\n")
        i = parse_identify(sample)
        return (i["did"] == 0x11001106 and i["flash_bytes"] == 0x40000 and
                classify_openocd_output("Error: unable to find a matching CMSIS-DAP device") is not None and
                parse_ioreg('+-o Atmel-ICE@1\n"idVendor" = 1003\n"idProduct" = 8513\n"USB Serial Number" = "J1"\n')[0]["serial"] == "J1")


class OpenocdFlash(_OcdBase):
    """params: image (ELF/HEX path, required), probe, target, target_cfg, interface, vid_pid, probe_serial, speed_khz, openocd, config, map: {flashed, image_sha256}.
    Programs with verify, resets, and proposes flashed=Y plus the image hash. A failed verify raises."""

    name = "openocd.flash"

    def run(self, ctx) -> None:
        p = ctx.params
        image = Path(str(need(p, "image"))).expanduser()
        if not image.is_file():
            raise ParamError(f"image not found: {image}")
        if image.suffix.lower() not in (".elf", ".hex", ".bin", ""):
            ctx.log(f"unusual image extension {image.suffix}", "warn")
        if image.suffix.lower() == ".bin":
            raise ParamError("a .bin has no load address; use the .hex or the ELF")
        self._begin(p)
        pname, probe = resolve_probe(p)
        resolve_target(p)
        tcl_word(image.resolve(), "image path", spaces=True)
        if p.get("probe_serial"):
            tcl_word(p["probe_serial"], "probe_serial")
        digest = sha256_file(image)
        mp = p.get("map") or {}
        ctx.log(f"image {image.name} sha256 {digest}")
        if ctx.simulate:
            ctx.progress(0.3, "simulated erase and program")
            ctx.progress(0.8, "simulated verify")
            ctx.log("simulated: verified OK")
        else:
            note = self._orphans(ctx)
            probes = usb_probes(pname)
            q = dict(p)
            if not q.get("probe_serial") and len(probes) == 1 and probes[0]["serial"]:
                q["probe_serial"] = usb_serial(probes[0]["serial"])
            if len(probes) > 1 and not q.get("probe_serial"):
                raise ProbeError(f"More than one {probe['name']} is connected; give probe_serial in the action params")
            ctx.progress(0.1, "programming and verifying")
            path = tcl_word(image.resolve(), "image path", spaces=True)
            cmds = [f'program "{path}" verify reset', "shutdown"]
            rc, out = self._run_noted(self._args(q, cmds), to_float(p.get("timeout_s", FLASH_TIMEOUT_S), "timeout_s"), ctx, note)
            ok = "** verified ok **" in out.lower()
            for ln in out.strip().splitlines()[-8:]:
                ctx.log(ln)
            if rc != 0 or not ok:
                msg = classify_openocd_output(out, *self._cls)
                raise ProbeError((msg + "\n" if msg else "") + "Flash did not verify. Last output:\n"
                                 + "\n".join(out.strip().splitlines()[-6:]) + note)
        if mp.get("flashed"):
            ctx.propose(mp["flashed"], "Y", None, None, f"verified {image.name}")
        if mp.get("image_sha256"):
            ctx.propose(mp["image_sha256"], digest, None, f"{image.name}", digest)
        ctx.progress(1.0, "done")

    def _selftest(self) -> bool:
        return classify_openocd_output("Error: Error connecting DP: cannot read IDR") is not None


class OpenocdReadRam(_OcdBase):
    """Read a block of target RAM over SWD (no halt, no reset) and propose fields of it as test values.
    A self-test image keeps its results in a RAM struct, so they are read with the probe and need no CAN adapter.
    params: address (int or hex) | map_file + symbol (a GNU ld .map, resolved at run time), length (bytes),
    ready: {byte: n, not: v} (poll until that byte differs from v, e.g. a verdict leaving 'running'), wait_s (20),
    fields: [{test, byte, bit} -> "1"/"0" | {test, byte} -> the byte value |
    {test, byte, width: 1|2|4, signed: false, endian: little|big, scale: 1, offset: 0, decimals, invalid, sim} -> raw*scale+offset
    as a decimal string; a raw value equal to `invalid` proposes nothing and logs a warning], plus the usual probe params."""

    name = "openocd.read_ram"

    @staticmethod
    def _symbol(map_file: str, symbol: str) -> int:
        txt = Path(str(map_file)).expanduser().read_text(errors="replace")
        m = re.search(r"^\s+0x([0-9a-fA-F]+)\s+" + re.escape(symbol) + r"\s*$", txt, re.M)
        if not m:
            raise ParamError(f"symbol {symbol} not found in {map_file}")
        return int(m.group(1), 16)

    @staticmethod
    def _numeric(f: dict, length: int) -> dict | None:
        """Validated spec of a multi-byte numeric field, or None for the byte/bit forms."""
        if "bit" in f or "width" not in f:
            return None
        width = to_int(f["width"], "width")
        if width not in (1, 2, 4):
            raise ParamError(f"field {f.get('test')}: width must be 1, 2 or 4, not {width}")
        endian = str(f.get("endian", "little")).lower()
        if endian not in ("little", "big"):
            raise ParamError(f"field {f.get('test')}: endian must be little or big")
        byte = to_int(f["byte"], "byte")
        if byte < 0 or byte + width > length:
            raise ParamError(f"field {f.get('test')}: bytes {byte}..{byte + width - 1} are outside the {length}-byte read")
        inv = f.get("invalid")
        return {"byte": byte, "width": width, "endian": endian, "signed": bool(f.get("signed", False)),
                "scale": to_float(f.get("scale", 1), "scale"), "offset": to_float(f.get("offset", 0), "offset"),
                "decimals": None if f.get("decimals") is None else to_int(f["decimals"], "decimals"),
                "invalid": None if inv is None else to_int(inv, "invalid")}

    def run(self, ctx) -> None:
        p = ctx.params
        if p.get("map_file") and p.get("symbol"):
            addr = self._symbol(p["map_file"], p["symbol"])
        else:
            addr = to_int(need(p, "address"), "address")
        length = to_int(p.get("length", 8), "length")
        fields = p.get("fields") or []
        specs = [self._numeric(f, length) for f in fields]
        ready = p.get("ready") or None
        wait_s = to_float(p.get("wait_s", 20), "wait_s")
        self._begin(p)
        if ctx.simulate:
            data = [0] * length
            if ready:
                data[int(ready["byte"])] = 1
            for f, sp in zip(fields, specs):
                if sp:
                    want = to_float(f.get("sim", 0), "sim")
                    raw = int(round((want - sp["offset"]) / sp["scale"])) if sp["scale"] else 0
                    nbits = 8 * sp["width"]
                    lo, hi = (-(1 << (nbits - 1)), (1 << (nbits - 1)) - 1) if sp["signed"] else (0, (1 << nbits) - 1)
                    raw = max(lo, min(hi, raw)) & ((1 << nbits) - 1)
                    data[sp["byte"]:sp["byte"] + sp["width"]] = list(raw.to_bytes(sp["width"], sp["endian"]))
                elif "bit" in f:
                    data[int(f["byte"])] |= 1 << int(f["bit"])
        else:
            note = self._orphans(ctx)
            q = dict(p)
            probes = usb_probes(resolve_probe(p)[0])
            if not q.get("probe_serial") and len(probes) == 1 and probes[0]["serial"]:
                q["probe_serial"] = usb_serial(probes[0]["serial"])
            t_end = time.time() + wait_s
            data = None
            while True:
                ctx.progress(0.3, f"reading {length} bytes at 0x{addr:08x} over SWD")
                rc, out = self._run_noted(self._args(q, ["init", f"catch {{mdb 0x{addr:08x} {length}}} e; echo $e", "shutdown"]),
                                          IDENTIFY_TIMEOUT_S, ctx, note)
                got = []
                for line in out.splitlines():
                    m = re.match(r"\s*0x([0-9a-fA-F]+):\s+((?:[0-9a-fA-F]{2}\s*)+)$", line)
                    if m:
                        got += [int(x, 16) for x in m.group(2).split()]
                if len(got) < length:
                    msg = classify_openocd_output(out, *self._cls)
                    raise ProbeError((msg or "OpenOCD ran but the RAM read returned nothing:\n" + "\n".join(out.strip().splitlines()[-6:])) + note)
                data = got[:length]
                if not ready or data[int(ready["byte"])] != int(ready.get("not", 0)) or time.time() >= t_end:
                    break
                ctx.log(f"byte {ready['byte']} is still {data[int(ready['byte'])]}; waiting for the firmware to finish")
                time.sleep(1.5)
            ctx.log("RAM: " + " ".join(f"{x:02x}" for x in data))
            if ready and data[int(ready["byte"])] == int(ready.get("not", 0)):
                ctx.log(f"still not ready after {wait_s:g} s; values proposed as read", "warn")
        for f, sp in zip(fields, specs):
            if sp:
                chunk = data[sp["byte"]:sp["byte"] + sp["width"]]
                raw = int.from_bytes(bytes(chunk), sp["endian"], signed=sp["signed"])
                hexs = "".join(f"{x:02x}" for x in chunk)
                if sp["invalid"] is not None and raw == sp["invalid"]:
                    ctx.log(f"{f['test']}: raw {raw} (0x{hexs}) is the 'not measured' value; nothing proposed", "warn")
                    continue
                val = raw * sp["scale"] + sp["offset"]
                txt = f"{val:.{sp['decimals']}f}" if sp["decimals"] is not None else f"{val:.12g}"
                ctx.propose(f["test"], txt, None, None,
                            f"bytes {sp['byte']}..{sp['byte'] + sp['width'] - 1} = 0x{hexs} ({sp['endian']}, "
                            f"{'signed' if sp['signed'] else 'unsigned'}) = {raw}; {raw} * {sp['scale']:g} + {sp['offset']:g}")
                continue
            b = data[int(f["byte"])]
            if "bit" in f:
                v = "1" if b >> int(f["bit"]) & 1 else "0"
                ctx.propose(f["test"], v, None, None, f"byte {f['byte']} = 0x{b:02x}, bit {f['bit']}")
            else:
                ctx.propose(f["test"], str(b), None, None, f"byte {f['byte']} = 0x{b:02x}")
