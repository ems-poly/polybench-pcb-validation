"""Actions `esptool.identify` and `esptool.flash`: Espressif chips over their USB / UART bootloader, with the esptool package.

Optional extra: `pip install -e '.[esp32]'` (esptool, pyserial). Without it the actions say so; the simulator needs neither.

esptool is run as a child process (`python -m esptool ...`) with a timeout owned here, in its own process group, so a hung
board never hangs the bench. Output is parsed into proposals; the action never decides pass or fail. Port choice is explicit
(`port` param) or found by USB id (Espressif native USB 303A:*, Silicon Labs CP210x 10C4:EA60, WCH CH340 1A86:7523 and
FTDI 0403:6001); more than one candidate is an error that names them, so esptool never probes unrelated serial devices.

Readiness: HOST-ONLY (parsers on captured-format text, simulator, fake esptool in tests). No board has been connected.
"""
from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from ._util import ParamError, PluginError, need, sha256_file, timeout_param, to_int

USB_IDS = {(0x303A, None): "Espressif native USB", (0x10C4, 0xEA60): "CP210x UART bridge",
           (0x1A86, 0x7523): "CH340 UART bridge", (0x0403, 0x6001): "FTDI UART"}
IDENTIFY_TIMEOUT_S = 25.0
FLASH_TIMEOUT_S = 150.0


class EspError(PluginError):
    pass


def candidate_ports() -> list[dict]:
    """Serial ports whose USB ids look like an ESP32 board: [{'device','desc','serial'}]. Empty when pyserial is missing."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    out = []
    for p in list_ports.comports():
        if p.vid is None:
            continue
        for (vid, pid), name in USB_IDS.items():
            if p.vid == vid and (pid is None or p.pid == pid):
                # macOS lists tty.* and cu.*; only the call-out device is used for opening
                if p.device.startswith("/dev/tty."):
                    break
                out.append({"device": p.device, "desc": name, "serial": p.serial_number or ""})
                break
    return out


def pick_port(params: dict) -> str:
    port = params.get("port") or os.environ.get("POLYBENCH_ESP_PORT")
    if port:
        return str(port)
    c = candidate_ports()
    if not c:
        raise EspError("No ESP32 board found on USB. Plug the board in with a Micro-USB data cable (not charge-only), into "
                       "the port marked UART (the USB-bridge port: flashing and the serial self-test both use it), or give "
                       "`port` in the action params (for example /dev/cu.usbserial-0001).")
    if len(c) > 1:
        raise EspError("More than one candidate serial port; give `port` in the action params: "
                       + ", ".join(f"{x['device']} ({x['desc']})" for x in c))
    return c[0]["device"]


def esptool_available() -> bool:
    """Presence check only. esptool is GPL-2.0-or-later: Polybench never imports it, it runs `python -m esptool` as a
    child process (find_spec imports nothing)."""
    import importlib.util
    try:
        return importlib.util.find_spec("esptool") is not None
    except (ImportError, ValueError):
        return False


def run_esptool(args: list[str], timeout: float, ctx=None) -> str | None:
    """Run `python -m esptool <args>` in its own process group; the timeout is enforced here. Returns merged output, or None when the operator cancelled (esptool is stopped first)."""
    if not esptool_available():
        raise EspError("The esptool package is not installed: install `polybench-pcb-validation[esp32]` to use this")
    p = subprocess.Popen([sys.executable, "-m", "esptool", *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, text=True, start_new_session=True)
    t0 = time.monotonic()
    while True:
        try:
            out, _ = p.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            pass
        if (ctx is not None and ctx.cancelled()) or time.monotonic() - t0 > timeout:
            cancelled = ctx is not None and ctx.cancelled()
            out = _stop(p)
            if cancelled:
                return None  # cancelled: the caller returns and the child ends with done/cancelled
            raise EspError(classify(out) or f"esptool did not finish in {timeout:.0f} s")
    if p.returncode != 0:
        raise EspError(classify(out) or "esptool failed:\n" + "\n".join(out.strip().splitlines()[-5:]))
    return out


def _stop(p) -> str:
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


def classify(text: str) -> str | None:
    t = (text or "").lower()
    if "failed to connect" in t or "no serial data received" in t or "wrong boot mode" in t or "timed out waiting for packet header" in t:
        return ("esptool could not get the chip to answer. Check the Micro-USB data cable and the port (UART port), and that "
                "no serial monitor holds it. If it still fails: hold BOOT, press and release RESET, release BOOT, then run "
                "again (this is for esptool only; press RESET alone afterwards to run the firmware).")
    if "could not open" in t or "resource busy" in t or "permission denied" in t or "device not configured" in t:
        return "The serial port cannot be opened. Close any serial monitor or other program using it, or replug the board."
    if "hash of data verified" not in t and "write_flash" in t and "a fatal error occurred" in t:
        return "esptool reported a fatal error while writing:\n" + "\n".join(t.strip().splitlines()[-3:])
    return None


def parse_identify(text: str) -> dict:
    """Chip, MAC, features and flash size from `esptool flash-id` / `flash_id` output (v4 and v5 wording)."""
    d: dict = {"chip": None, "mac": None, "features": None, "flash_mb": None, "psram_mb": None}
    m = re.search(r"Chip (?:type|is)\s*:?\s*(ESP32[-\w]*)", text)
    if m:
        d["chip"] = m.group(1)
    m = re.search(r"Chip type:\s*(.+)", text)
    d["chip_full"] = m.group(1).strip() if m else None
    m = re.search(r"MAC:\s*((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})", text)
    if m:
        d["mac"] = m.group(1).lower()
    m = re.search(r"Features:\s*(.+)", text)
    if m:
        d["features"] = m.group(1).strip()
        mp = re.search(r"PSRAM\s*(\d+)\s*MB", d["features"], re.I)
        if mp:
            d["psram_mb"] = int(mp.group(1))
    m = re.search(r"Detected flash size:\s*(\d+)\s*MB", text, re.I)
    if m:
        d["flash_mb"] = int(m.group(1))
    return d


class _Base:
    capabilities = ["programmer"]
    name = ""

    def safe_state(self) -> str:
        return "no esptool process owned by this action after it exits; the board is left running its firmware (hard reset)"


class EsptoolIdentify(_Base):
    """params: port (optional), baud (115200), timeout_s, map: {chip, mac, flash_mb, psram_mb, port} -> test ids.
    Reads chip type, MAC, embedded PSRAM (from the chip features) and flash size (esptool flash-id). Changes nothing."""

    name = "esptool.identify"

    def run(self, ctx) -> None:
        p = ctx.params
        mp = p.get("map") or {}
        timeout_param(p.get("timeout_s", IDENTIFY_TIMEOUT_S))
        if ctx.simulate:
            for f, t in ((0.2, "simulated: opening the port"), (0.5, "simulated: asking the chip")):
                if ctx.cancelled():
                    ctx.log("cancelled; nothing proposed", "warn")
                    return
                ctx.progress(f, t)
                time.sleep(0.2)
            if ctx.cancelled():
                ctx.log("cancelled; nothing proposed", "warn")
                return
            h = hashlib.sha256(ctx.board.encode()).digest()
            info = {"chip": "ESP32-S3", "chip_full": "ESP32-S3 (QFN56) (revision v0.2)",
                    "mac": "7c:df:a1:%02x:%02x:%02x" % (h[0], h[1], h[2]), "features": "Wi-Fi, BLE, Embedded PSRAM 8MB (AP_3v3)",
                    "flash_mb": 16, "psram_mb": 8}
            port = "(simulated)"
            ctx.log("simulated: ESP32-S3 on a simulated port")
        else:
            port = pick_port(p)
            ctx.progress(0.1, f"asking the chip on {port}")
            out = run_esptool(["--port", port, "--baud", str(to_int(p.get("baud", 115200), "baud")), "flash-id"],
                              timeout_param(p.get("timeout_s", IDENTIFY_TIMEOUT_S)), ctx)
            if out is None:
                return
            info = parse_identify(out)
            ev = Path(ctx.workdir) / f"{ctx.board}_esptool_identify_{time.strftime('%Y%m%d_%H%M%S')}.txt"
            ev.parent.mkdir(parents=True, exist_ok=True)
            ev.write_text(out)
            if not info["chip"]:
                raise EspError("esptool ran but no chip type was found in its output; see " + str(ev))
            ctx.log(f"evidence: {ev}")
        ctx.log(f"{info['chip_full'] or info['chip']}; MAC {info['mac']}; flash {info['flash_mb']} MB; PSRAM {info['psram_mb']} MB")
        ctx.progress(0.9, "reporting")
        for key, val, unit in (("chip", info["chip"], None), ("mac", info["mac"], None), ("flash_mb", info["flash_mb"], "MB"),
                               ("psram_mb", info["psram_mb"], "MB"), ("port", port, None)):
            if mp.get(key) and val is not None:
                ctx.propose(mp[key], str(val), unit, None, str(val))
            elif mp.get(key):
                ctx.log(f"{key} was not reported by the chip; not proposed", "warn")
        ctx.progress(1.0, "done")

    def selftest(self) -> bool:
        s = ("Connected to ESP32-S3 on /dev/cu.usbmodem1101:\nChip type:          ESP32-S3 (QFN56) (revision v0.2)\n"
             "Features:           Wi-Fi, BLE, Embedded PSRAM 8MB (AP_3v3)\nMAC:                7c:df:a1:00:42:5e\n"
             "Detected flash size: 16MB\n")
        i = parse_identify(s)
        return i["chip"] == "ESP32-S3" and i["mac"] == "7c:df:a1:00:42:5e" and i["flash_mb"] == 16 and i["psram_mb"] == 8


class EsptoolFlash(_Base):
    """params: image (path; relative paths resolve against the server's working directory), offset ('0x0' for a merged
    factory image), simulate_without_image (bool, simulator only: with a pinned sha256 and no image file, run on and use that hash as the digest), sha256 (optional: the expected SHA-256 of the image; a mismatch refuses to flash, and `polybench doctor` reads it once its image check is merged), port, baud (460800), timeout_s (150), map: {flashed, image_sha256} -> test ids.
    Writes with esptool write-flash (which verifies by MD5 of the written region), then hard-resets. Proposes flashed = Y and
    the image SHA-256 only after esptool reports the hash verified; anything else raises and proposes nothing."""

    name = "esptool.flash"

    def run(self, ctx) -> None:
        p = ctx.params
        mp = p.get("map") or {}
        image = Path(str(need(p, "image"))).expanduser()
        want = p.get("sha256")
        # Simulator only: a pinned `sha256` plus `simulate_without_image: true` lets the run go on without the binary (a wheel does
        # not carry it), and the pinned hash stands in for the digest. A real flash always needs the file.
        if ctx.simulate and p.get("simulate_without_image") and want and not image.is_file():
            ctx.log(f"simulated: {image.name} is not on this machine; the protocol's pinned sha256 stands in for the digest "
                    "(nothing was hashed, a real flash needs the file)", "warn")
            absent = True
        else:
            absent = False
        if not absent and not image.is_file():
            raise ParamError(f"image not found: {image} (relative paths resolve against the directory polybench was started in)")
        if image.suffix.lower() not in (".bin", ".img"):
            raise ParamError("esptool.flash takes a raw .bin; for ELF use the PlatformIO upload")
        offset = to_int(p.get("offset", 0), "offset")
        timeout_param(p.get("timeout_s", FLASH_TIMEOUT_S))
        if absent:
            digest = str(want).strip().lower()
        else:
            digest = sha256_file(image)
            if want and str(want).strip().lower() != digest:
                raise ParamError(f"image SHA-256 {digest} does not match the protocol's sha256 {str(want).strip().lower()}; refusing to flash")
            ctx.log(f"image {image.name} ({image.stat().st_size} bytes) at 0x{offset:x}, sha256 {digest}")
        if ctx.simulate:
            for f, t in ((0.2, "simulated erase"), (0.6, "simulated write"), (0.9, "simulated verify")):
                if ctx.cancelled():
                    ctx.log("cancelled: nothing proposed (simulated; a real flash would be partly written)", "warn")
                    return
                ctx.progress(f, t)
                time.sleep(0.2)
            if ctx.cancelled():
                ctx.log("cancelled: nothing proposed (simulated)", "warn")
                return
            ctx.log("simulated: hash of data verified")
        else:
            port = pick_port(p)
            ctx.progress(0.1, f"writing {image.name} on {port}")
            out = run_esptool(["--port", port, "--baud", str(to_int(p.get("baud", 460800), "baud")), "write-flash",
                               f"0x{offset:x}", str(image)], timeout_param(p.get("timeout_s", FLASH_TIMEOUT_S)), ctx)
            if out is None:
                ctx.log("cancelled: esptool was stopped; the flash may be partly written, flash again before trusting it", "warn")
                return
            ev = Path(ctx.workdir) / f"{ctx.board}_esptool_flash_{time.strftime('%Y%m%d_%H%M%S')}.txt"
            ev.parent.mkdir(parents=True, exist_ok=True)
            ev.write_text(out)
            if "hash of data verified" not in out.lower():
                raise EspError("esptool finished without 'Hash of data verified'; not claiming the flash is good. See " + str(ev))
        ctx.progress(1.0, "verified")
        if mp.get("flashed"):
            ctx.propose(mp["flashed"], "Y", None, None, "hash of data verified")
        if mp.get("image_sha256"):
            ctx.propose(mp["image_sha256"], digest, None, None, digest)

    def selftest(self) -> bool:
        return classify("A fatal error occurred: Failed to connect to ESP32-S3") is not None
