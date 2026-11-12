"""Discovery of actions and ECAD bridges (entry points) plus the built-in demo ones."""
from __future__ import annotations

import importlib.metadata as md


BUILTIN = {"aoi.run": "polybench.plugins.aoi:AoiRun", "aoi.ls_export": "polybench.plugins.aoi:AoiLsExport"}


def _eps(group):
    try:
        return list(md.entry_points(group=group))
    except Exception:
        return []


def _instantiate(obj):
    if isinstance(obj, type):
        return obj()
    if callable(obj) and not hasattr(obj, "run") and not hasattr(obj, "highlight"):
        return obj()
    return obj


def action_names() -> set[str]:
    from . import demo_actions
    names = set(demo_actions.ACTIONS) | set(BUILTIN)
    for ep in _eps("polybench.actions"):
        names.add(ep.name)
    return names


def find_action(name: str):
    """Return an Action instance. Entry points win over built-ins. Raises KeyError."""
    for ep in _eps("polybench.actions"):
        if ep.name == name:
            obj = _instantiate(ep.load())
            if not getattr(obj, "name", None):
                try:
                    obj.name = name
                except Exception:
                    pass
            return obj
    if name in BUILTIN:     # shipped with the core, no entry point needed (an existing editable install keeps working)
        import importlib
        mod, cls = BUILTIN[name].split(":")
        return getattr(importlib.import_module(mod), cls)()
    from . import demo_actions
    if name in demo_actions.ACTIONS:
        return demo_actions.ACTIONS[name]()
    raise KeyError(f"no action plugin named '{name}' is installed")


def load_ecad(preferred: str | None = None, simulate: bool = False):
    """Return (bridge, note). Simulated bridge when --simulate or none is installed."""
    from .ecad_sim import SimulatedEcad
    import os
    preferred = preferred or os.environ.get("POLYBENCH_ECAD") or None        # KC-04: --ecad kicad or POLYBENCH_ECAD=kicad; default stays fusion
    if simulate and preferred == "kicad":
        from .kicad.bridge import KicadBridge
        return KicadBridge(simulate=True), "simulated KiCad (--simulate --ecad kicad)"
    if simulate:
        return SimulatedEcad(), "simulated ECAD (--simulate)"
    eps = _eps("polybench.ecad")
    pick = None
    for ep in eps:
        if preferred and ep.name == preferred:
            pick = ep
    if pick is None:
        pick = next((e for e in eps if e.name == "fusion"), eps[0] if eps else None)
    if pick is None:
        return SimulatedEcad(note="no ECAD bridge installed; highlighting is simulated"), "no ECAD bridge installed"
    try:
        return _instantiate(pick.load()), f"ecad bridge '{pick.name}'"
    except Exception as e:  # keep the app usable
        return SimulatedEcad(note=f"ECAD bridge '{pick.name}' failed to load: {e}"), f"bridge load failed: {e}"
