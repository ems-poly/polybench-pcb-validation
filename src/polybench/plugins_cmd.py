"""`polybench plugins list [--md] [--core]`: every registered action and ECAD bridge with its readiness label.

Labels come from the recorded evidence in docs/PLUGINS.md (READINESS below mirrors it, one line per plug-in) and are never
upgraded here. A simulator never validates hardware. A plug-in with no record (including every third-party plug-in) is
UNVALIDATED. The plug-ins are listed from their entry points without importing them, so a missing optional dependency
never breaks the list.
"""
from __future__ import annotations

import argparse
import sys

from . import registry

LABELS = ("HARDWARE-VALIDATED", "HOST-ONLY", "UNVALIDATED")
NO_RECORD = "No readiness recorded: treat as unvalidated until a real run is recorded."

# name -> (label, evidence). Source of each line: docs/PLUGINS.md (per-plug-in section, ECAD table and the readiness
# table) or the plug-in's own docstring. Where two records differ, the weaker label is used.
READINESS: dict[str, tuple[str, str]] = {
    "fusion": ("HARDWARE-VALIDATED", "15 Oct 2026: status, designators and highlight accepted, then cleared. The highlight was not inspected by eye."),
    "kicad": ("HARDWARE-VALIDATED", "17 Oct 2026, KiCad 10.0.7, read-only. Busy, no-board and KiCad-quit states are UNVALIDATED live."),
    "altium": ("UNVALIDATED", "Interface only (stub). It reports not installed and does nothing."),
    "openocd.identify": ("HOST-ONLY", "Fake OpenOCD. The no-probe path ran against real OpenOCD 0.12 (HARDWARE-VALIDATED); the target-found and no-response paths are UNVALIDATED."),
    "openocd.flash": ("HOST-ONLY", "Never flashed a board."),
    "openocd.read_ram": ("HOST-ONLY", "Fake OpenOCD only."),
    "pcan.capture": ("UNVALIDATED", "Against a node: the adapter enumerates; the first open was blocked by another program. Bit extraction is checked on the host."),
    "pcan.stats": ("UNVALIDATED", "Against a node: not yet run. Host tests only."),
    "pcan.query": ("UNVALIDATED", "Against a node: not yet run. Host tests only."),
    "n6705b.identify": ("HARDWARE-VALIDATED", "One *IDN? over LAN returned the identity and modules; no output command was sent. Needs the unpublished polystage-bench library."),
    "n6705b.measure": ("HOST-ONLY", "Simulator; outputs never switched on in a real run."),
    "n6705b.first_power": ("HOST-ONLY", "Simulator; outputs never switched on in a real run."),
    "n6705b.load_sweep": ("HOST-ONLY", "Simulator; outputs never switched on in a real run."),
    "camera.snapshot": ("HOST-ONLY", "Host tests only."),
    "dmm.read": ("HOST-ONLY", "Simulated meter. Device mechanism and OL. handling HARDWARE-VALIDATED on one UT161E (Ohm dial, open leads); numeric readings and other modes UNVALIDATED."),
    "esptool.identify": ("HOST-ONLY", "Parsers, simulator and a fake esptool. No board has been connected."),
    "esptool.flash": ("HOST-ONLY", "Parsers, simulator and a fake esptool. No board has been connected."),
    "serial.query": ("HOST-ONLY", "Simulator and a fake serial device in tests."),
    "aoi.run": ("UNVALIDATED", "No readiness recorded in docs/PLUGINS.md. Software image analysis; the operator's Y / N decides."),
    "aoi.ls_export": ("UNVALIDATED", "No readiness recorded in docs/PLUGINS.md."),
}
DEMO_NOTE = "Demo action for the DEMO-1 protocol and the simulator. No hardware exists behind it."


def _rows(core_only: bool = False) -> list[dict]:
    """One row per plug-in class that can run, plus one flagged row per package shadowed by a core name (it never runs).
    Readiness belongs to the core class only: a row whose package is not polybench is always UNVALIDATED."""
    rows: dict[tuple, dict] = {}

    def add(kind, name, target, package, shadowed=False):
        core = package == "polybench"
        if core_only and not core:
            return
        label, ev = READINESS.get(name, ("UNVALIDATED", NO_RECORD)) if core else ("UNVALIDATED", NO_RECORD)
        if shadowed:
            ev = f"SHADOWED: the name is a core plug-in, so the core class runs and this package does not. {NO_RECORD}"
        rows[(kind, name, package, shadowed)] = {"name": name, "kind": kind, "label": label, "evidence": ev,
                                                  "package": package, "target": target, "shadowed": shadowed}

    for group, kind in (("polybench.actions", "action"), ("polybench.ecad", "ecad")):
        for ep in registry._eps(group):
            dist = getattr(getattr(ep, "dist", None), "name", None) or "unknown"
            add(kind, ep.name, ep.value, "polybench" if registry.is_core(ep) else dist)
        for ep in registry.shadowed_eps(group):
            dist = getattr(getattr(ep, "dist", None), "name", None) or "unknown"
            add(kind, ep.name, ep.value, "polybench" if registry.is_core(ep) else dist, shadowed=True)
    for name, target in registry.BUILTIN.items():
        if ("action", name, "polybench", False) not in rows:
            add("action", name, target, "polybench")
    from . import demo_actions
    for name in demo_actions.ACTIONS:
        if not any(k[0] == "action" and k[1] == name for k in rows):
            rows[("action", name, "polybench", False)] = {"name": name, "kind": "action", "label": "HOST-ONLY", "evidence": DEMO_NOTE,
                                                          "package": "polybench", "target": "polybench.demo_actions", "shadowed": False}
    return sorted(rows.values(), key=lambda r: (r["kind"] != "ecad", r["name"], r["shadowed"]))


def render_text(rows: list[dict]) -> str:
    w = max([len(r["name"]) for r in rows] + [4])
    out = [f"{'NAME':<{w}}  {'KIND':<6}  {'READINESS':<18}  PACKAGE"]
    for r in rows:
        out.append(f"{r['name']:<{w}}  {r['kind']:<6}  {r['label']:<18}  {r['package']}" + ("  SHADOWED (core name; does not run)" if r["shadowed"] else ""))
    return "\n".join(out) + "\n"


def render_md(rows: list[dict]) -> str:
    out = ["| Plug-in | Kind | Readiness | Evidence | Package |", "|---|---|---|---|---|"]
    for r in rows:
        kind = "ECAD bridge" if r["kind"] == "ecad" else "action"
        out.append(f"| `{r['name']}`{' (shadowed)' if r['shadowed'] else ''} | {kind} | {r['label']} | {r['evidence'].replace('|', chr(92) + '|')} | {r['package']} |")
    return "\n".join(out) + "\n"


EQUIPMENT_HEAD = """# Supported equipment

Generated by `polybench plugins list --md --core`. Do not edit by hand: a test fails when this file is stale. Regenerate with
`polybench plugins list --md --core > docs/SUPPORTED_EQUIPMENT.md`.

Labels: HARDWARE-VALIDATED means the plug-in did what it should against the real instrument; HOST-ONLY means it passed on a
computer with a simulator or a fake; UNVALIDATED means a real run is still to do, or nothing is recorded. A simulator never
validates hardware. A plug-in that is not listed here, including every third-party plug-in, has no recorded readiness and
counts as UNVALIDATED. The evidence behind each line is in `docs/PLUGINS.md`. To get a plug-in listed, see "Sharing a plug-in" there.

"""


def equipment_doc() -> str:
    return EQUIPMENT_HEAD + render_md(_rows(core_only=True))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="polybench plugins", description="list registered plug-ins and their readiness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="every registered action and ECAD bridge with its readiness label")
    ls.add_argument("--md", action="store_true", help="a Markdown table (docs/SUPPORTED_EQUIPMENT.md)")
    ls.add_argument("--core", action="store_true", help="only the plug-ins shipped with Polybench; with --md, the full generated document")
    a = ap.parse_args(argv)
    rows = _rows(core_only=a.core)
    if a.md:
        sys.stdout.write(equipment_doc() if a.core else render_md(rows))
    else:
        sys.stdout.write(render_text(rows))
    return 0
