"""Bench check, stage 1 of an automated step (docs/AUTOMATED_STEP.md, AS-14).

The protocol's `equipment:` list says what the step needs. Each entry is checked with the checks `polybench doctor` already has (USB
listing, PCAN open in listen-only mode, the N6705B *IDN?), read-only: no target attach, no CAN frame transmitted, no output touched.
Blocking calls: run in a thread. Under --simulate nothing is touched; an entry's `sim: {status: missing, detail}` rehearses a missing item.
"""
from __future__ import annotations

import glob
import os
import re

from . import doctor

OK, WARN, FAIL, INFO = doctor.OK, doctor.WARN, doctor.FAIL, doctor.INFO
KINDS = ("openocd_probe", "pcan", "serial", "usb", "n6705b", "manual")
PURPOSE = {"openocd_probe": "SWD programming", "pcan": "CAN results", "serial": "UART console", "usb": "USB device", "n6705b": "power analyzer", "manual": ""}
NAME = {"openocd_probe": "Debug probe", "pcan": "PCAN adapter", "serial": "Serial port", "usb": "USB device", "n6705b": "Power analyzer", "manual": "Equipment"}


def parse_vid_pid(v):
    """'03eb:2141' | '0x03eb:0x2141' | [vid, pid] -> (vid, pid) or None."""
    if isinstance(v, str):
        v = v.replace(" ", "").split(":")
    return doctor._vid_pid(v) if v else None


def _sim(item: dict) -> dict:
    sim = item.get("sim") or {}
    kind = item["kind"]
    ok = {"openocd_probe": "Atmel-ICE J41800012345, CMSIS-DAP (simulated)", "pcan": f"PCAN-USB found, {item.get('channel', 'PCAN_USBBUS1')} open at "
          f"{float(item.get('bitrate', 1000000)) / 1e6:g} Mbit/s, bus state ACTIVE (simulated)", "serial": "/dev/cu.usbserial-SIM open (simulated)",
          "usb": "device present (simulated)", "n6705b": "N6705B answered *IDN? (simulated)"}[kind]
    if sim.get("status") == "missing":
        return {"status": FAIL, "detail": sim.get("detail") or f"{NAME[kind]} not found (simulated)", "hint": sim.get("hint") or "plug it in, then check again"}
    return {"status": OK, "detail": ok, "hint": ""}


def _flat(item: dict) -> dict:
    """The contract puts the matching keys under `match:` (a dict: vid_pid, regex, pattern; or, for a serial port, the regex itself).
    Older entries have them at the top. Both work."""
    m = item.get("match")
    out = dict(item)
    if isinstance(m, dict):
        out.update({k: v for k, v in m.items() if k not in out or k in ("vid_pid",)})
        if m.get("regex") or m.get("pattern"):
            out["match"] = m.get("regex") or m.get("pattern")
        else:
            out.pop("match", None)
    return out


def check_item(item: dict, devs, simulate: bool) -> dict:
    meta = {k: item[k] for k in ("connections", "notes", "image") if item.get(k)}
    item = _flat(item)
    kind = item["kind"]
    if kind == "manual":                                    # nothing to detect: the operator ticks "connected"
        r = {"status": INFO, "detail": "no automatic check: tick it when it is connected", "hint": ""}
    elif kind not in KINDS:                                   # a plug-in's own equipment kind: no host-side check, the operator confirms it
        r = {"status": INFO, "detail": "no automatic check for this kind: check it by eye", "hint": ""}
    elif simulate:
        r = _sim(item)
    else:
        sev = FAIL
        if kind == "openocd_probe":
            c = doctor.check_probe(devs, item.get("probe", "atmel-ice"), sev, item.get("probe_serial"), parse_vid_pid(item.get("vid_pid")))
        elif kind == "pcan":
            c = doctor.check_pcan(devs, True, item.get("channel", "PCAN_USBBUS1"), sev, int(item.get("bitrate", 1000000)))
        elif kind == "usb":
            vp = parse_vid_pid(item.get("vid_pid"))
            hit = [d for d in (devs or []) if vp and (d.get("vid"), d.get("pid")) == vp]
            c = doctor.Check(OK if hit else FAIL, "USB device", (hit[0].get("name", "device") + " on USB") if hit else f"{item.get('vid_pid')} not on USB",
                             "" if hit else "plug it in")
        elif kind == "serial":
            rx = re.compile(item.get("match") or ".")
            ports = sorted(p for p in glob.glob("/dev/cu.*") if rx.search(p))
            if not ports:
                c = doctor.Check(FAIL, "Serial port", f"no port matches {item.get('match')!r}", "plug in the USB-serial adapter")
            elif not os.access(ports[0], os.R_OK | os.W_OK):
                c = doctor.Check(FAIL, "Serial port", f"{ports[0]} exists but cannot be opened (permissions)", "")
            else:
                c = doctor.Check(OK, "Serial port", f"{ports[0]} present, {item.get('baud', 115200)} baud (not opened: opening can reset a board)" +
                                 (f"; {len(ports)} match, using the first" if len(ports) > 1 else ""), "")
        else:  # n6705b
            c = doctor.check_n6705b(item.get("host"))
            if c.status == WARN:
                c.status = FAIL
        r = {"status": c.status, "detail": c.detail, "hint": c.hint}
    return {"id": item["id"], "kind": kind, "name": item.get("title") or NAME.get(kind, kind), "purpose": item.get("purpose") or item.get("for") or PURPOSE.get(kind, ""),
            "required": item.get("required", True) is not False, **meta, **r,
            "state": "manual" if kind == "manual" else {OK: "ok", WARN: "warn", INFO: "ok", FAIL: "missing"}.get(r["status"], "missing")}


def run_checks(equipment: list[dict], simulate: bool) -> list[dict]:
    devs = None if simulate or not any(e["kind"] in ("openocd_probe", "pcan", "usb") for e in equipment) else doctor.usb_devices()
    out = []
    for e in equipment:
        try:
            out.append(check_item(e, devs, simulate))
        except Exception as ex:  # noqa: BLE001  (a check must never stop the page)
            out.append({"id": e["id"], "kind": e["kind"], "name": e.get("title") or NAME.get(e["kind"], e["kind"]), "purpose": e.get("purpose") or e.get("for") or "", "required": True,
                        "status": FAIL, "state": "missing", "detail": f"the check failed: {type(ex).__name__}: {ex}", "hint": ""})
    return out


def blocking(results: list[dict], skips: dict, ticked=()) -> list[dict]:
    """Required items that are missing (or a manual item not ticked) and not skipped with a typed reason. Any item may be skipped."""
    return [r for r in results if r["required"] and not str(skips.get(r["id"], "")).strip()
            and (r["state"] == "missing" or (r["state"] == "manual" and r["id"] not in ticked))]
