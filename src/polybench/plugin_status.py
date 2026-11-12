"""What the Plugins panel shows: every discovered action and ECAD bridge, its status, whether the loaded protocol uses it,
the settings the protocol passes to it (read-only, secrets masked) and its last result in this batch's journal.

No hardware side effects (P4): status comes from `find_spec`, a module import (never an instance, never `run`) and the
journal. Nothing here opens a port, probes USB, connects to an instrument or calls an ECAD program.
"""
from __future__ import annotations

import importlib.metadata as md
import importlib.util
import re
from pathlib import Path

from . import extras, registry

OUR_DIST = extras.PACKAGE
SECRET_KEY = re.compile(r"key|token|password|secret", re.I)
MASK = "********"
# plug-in name prefix -> what it needs besides the core install: ("extra", extra name, module to find) or ("lib", import name, text)
NEEDS = {
    "aoi": ("extra", "aoi", None),
    "camera": ("extra", "camera", None),
    "esptool": ("extra", "esp32", None),
    "serial": ("extra", "esp32", None),
    "dmm": ("extra", "dmm", None),
    "n6705b": ("lib", "polystage_bench", "the separate polystage-bench library (unpublished, not on PyPI)"),
}
ECAD_NEEDS = {"kicad": ("extra", "kicad", "kipy")}
_cache: dict[str, list[dict]] = {}


def mask(x, key: str = ""):
    """Copy of a settings value with every value under a secret-looking key replaced."""
    if isinstance(x, dict):
        return {k: (MASK if SECRET_KEY.search(str(k)) else mask(v, str(k))) for k, v in x.items()}
    if isinstance(x, list):
        return [mask(v, key) for v in x]
    return x


def _has(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def _needs_status(need) -> dict | None:
    kind = need[0]
    if kind == "extra":
        extra, mod = need[1], need[2]
        ok = (_has(mod) if mod else extras.check(extra))
        if ok:
            return None
        return {"status": "missing_extra", "detail": extras.message(extra) if extra in extras.WHAT else f"install the `{extra}` extra",
                "install": {"extra": extra, "pip": extras.install_command(extra), "pipx": extras.pipx_command(extra)}}
    mod, text = need[1], need[2]
    if _has(mod):
        return None
    return {"status": "missing_library", "detail": f"needs {text}", "install": None}


def _source(dist) -> dict:
    if dist is None or dist.metadata["Name"] == OUR_DIST:
        return {"type": "builtin", "label": "built-in", "dist": OUR_DIST, "version": _our_version()}
    return {"type": "entry_point", "label": f"{dist.metadata['Name']} {dist.version}", "dist": dist.metadata["Name"], "version": dist.version}


def _our_version() -> str | None:
    try:
        return md.version(OUR_DIST)
    except md.PackageNotFoundError:
        return None


def _import_check(target) -> tuple[bool, str]:
    """Import the module that holds the plug-in. Never instantiates it."""
    try:
        if isinstance(target, str):
            import importlib
            importlib.import_module(target.split(":")[0])
        else:
            target.load()
        return True, ""
    except Exception as e:  # noqa: BLE001
        msg = " ".join(str(e).split())[:140]
        return False, f"{type(e).__name__}: {msg}"


def _status(name: str, kind: str, target) -> dict:
    need = (ECAD_NEEDS if kind == "ecad" else NEEDS).get(name.split(".")[0])
    if need:
        s = _needs_status(need)
        if s:
            return s
    ok, err = _import_check(target)
    return {"status": "loaded", "detail": "", "install": None} if ok else {"status": "import_error", "detail": err, "install": None}


def discover(rescan: bool = False) -> list[dict]:
    """Static part: every action and ECAD bridge with source and status. Cached for the life of the process (an install needs a restart)."""
    if "rows" in _cache and not rescan:
        return [dict(r) for r in _cache["rows"]]
    from . import demo_actions
    rows: dict[tuple[str, str], dict] = {}
    for n in sorted(demo_actions.ACTIONS):
        rows[("action", n)] = {"name": n, "kind": "action", "source": {"type": "builtin", "label": "built-in (demo)", "dist": OUR_DIST, "version": _our_version()},
                               **_status(n, "action", demo_actions.__name__)}
    for n, tgt in sorted(registry.BUILTIN.items()):
        rows[("action", n)] = {"name": n, "kind": "action", "source": {"type": "builtin", "label": "built-in", "dist": OUR_DIST, "version": _our_version()},
                               **_status(n, "action", tgt)}
    for kind, group in (("action", "polybench.actions"), ("ecad", "polybench.ecad")):
        for ep in registry._eps(group):
            rows[(kind, ep.name)] = {"name": ep.name, "kind": kind, "source": _source(getattr(ep, "dist", None)), **_status(ep.name, kind, ep)}
    _cache["rows"] = sorted(rows.values(), key=lambda r: (r["kind"] != "action", r["name"]))
    return [dict(r) for r in _cache["rows"]]


def _action_files(proto) -> dict[tuple, str]:
    """(step id, section id, action id) -> the YAML file of the protocol (top file or an include) that declares it. The loaded protocol
    is merged, so the file is found by reading each protocol file again (read-only). A plan-generated step has no entry."""
    import yaml
    out: dict[tuple, str] = {}
    for f in getattr(proto, "files", []) or []:
        if str(f).rsplit(".", 1)[-1].lower() not in ("yaml", "yml"):
            continue
        try:
            doc = yaml.safe_load(Path(f).read_text(encoding="utf-8")) or {}
            for st in (doc.get("steps") or []) if isinstance(doc, dict) else []:
                for sec in st.get("sections") or []:
                    for a in sec.get("actions") or []:
                        out.setdefault((st.get("id"), sec.get("id"), a.get("id")), str(f))
        except Exception:  # noqa: BLE001  (an unreadable file only loses the attribution)
            continue
    return out


def _protocol_uses(proto, ws_file: str | None) -> dict[str, list[dict]]:
    """plugin name -> [{step, section, action, file, params}] as the current protocol passes them."""
    out: dict[str, list[dict]] = {}
    if proto is None:
        return out
    where = _action_files(proto)
    for st in proto.data["steps"]:
        for sec in st.get("sections", []) or []:
            for a in sec.get("actions", []) or []:
                if not a.get("plugin"):
                    continue
                f = where.get((st.get("id"), sec.get("id"), a.get("id"))) or str(proto.path)
                out.setdefault(str(a["plugin"]), []).append({"step": st.get("id"), "section": sec.get("id") or sec.get("title"), "action": a.get("id"),
                                                             "file": str(f), "params": mask(dict(a.get("params") or {}))})
    return out


def last_results(events: list[dict]) -> dict[str, dict]:
    """plugin -> the last action in this batch's journal: {state, ts, board, text}. A run with no end event reads 'started'."""
    runs: dict = {}
    last: dict[str, dict] = {}
    for e in events:
        t, p = e["type"], e["payload"]
        if t == "action_start":
            runs[p.get("run")] = e
            last[p["plugin"]] = {"state": "started", "ts": e["ts"], "board": p.get("board"), "text": "", "run": p.get("run")}
        elif t == "action_end":
            s = runs.get(p.get("run"))
            if s:
                last[s["payload"]["plugin"]] = {"state": p.get("state"), "ts": e["ts"], "board": s["payload"].get("board"),
                                                "text": str(p.get("text") or "")[:160], "run": p.get("run")}
    return last


def build(sess=None, rescan: bool = False) -> dict:
    """The /api/plugins body. `sess` is the open Session, or None (welcome page: no protocol, no journal)."""
    rows = discover(rescan)
    uses = _protocol_uses(sess.proto, None) if sess else {}
    last = last_results(sess.journal.events) if sess else {}
    ecad_id = sess.ecad_id if sess else None
    sel = (sess.engine.state.project.get("select") if sess else None) or None
    for r in rows:
        if r["kind"] == "action":
            u = uses.get(r["name"], [])
            r["used"] = bool(u)
            r["settings"] = [{"where": f"{x['step']} / {x['section']} / {x['action']}", "file": x["file"], "params": x["params"]} for x in u]
            r["last"] = last.get(r["name"])
        else:
            r["used"] = bool(sess) and r["name"] == ecad_id
            r["settings"] = [{"where": "project ECAD platform", "file": str(sess.workspace.file), "params": {"program": ecad_id}}] if r["used"] else []
            r["last"] = ({"state": "document selected", "ts": sel.get("ts"), "board": None, "text": str(sel.get("document") or "")}
                         if r["used"] and sel and sel.get("ts") else None)
    attention = sum(1 for r in rows if r["used"] and r["status"] != "loaded")
    return {"protocol": str(sess.proto.path) if sess else None, "plugins": rows,
            "summary": {"total": len(rows), "loaded": sum(r["status"] == "loaded" for r in rows), "attention": attention,
                        "errors": sum(1 for r in rows if r["used"] and r["status"] == "import_error"),
                        "unavailable_unused": sum(1 for r in rows if not r["used"] and r["status"] != "loaded")}}


def summary(sess=None) -> dict:
    """The chip's counts. It rides on every /api/state, so it reads neither the protocol files nor the journal: only which plugins
    the loaded protocol names (in memory) and the cached discovery."""
    rows = discover()
    used = set()
    if sess:
        used = {str(a["plugin"]) for st in sess.proto.data["steps"] for sec in st.get("sections", []) or []
                for a in sec.get("actions", []) or [] if a.get("plugin")}
        used.add(("ecad", sess.ecad_id))
    is_used = lambda r: (r["name"] in used) if r["kind"] == "action" else (("ecad", r["name"]) in used)
    return {"total": len(rows), "loaded": sum(r["status"] == "loaded" for r in rows),
            "attention": sum(1 for r in rows if is_used(r) and r["status"] != "loaded"),
            "errors": sum(1 for r in rows if is_used(r) and r["status"] == "import_error"),
            "unavailable_unused": sum(1 for r in rows if not is_used(r) and r["status"] != "loaded")}
