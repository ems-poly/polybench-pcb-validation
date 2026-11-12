"""Project folder, project settings file, recent projects and the server-side folder browser (step 1, card 0).

Model : one project folder per design, one sub-folder per batch inside it. The project folder holds
`polybench-project.json` (ECAD platform, ECAD document, BOM file, protocol file); each batch folder holds its journal, runs, AOI
images and voice clips. The settings file references the protocol and BOM paths, it never copies them. A folder of batch folders
without a settings file (the layout before this change, e.g. ~/polybench-data) is a legacy project: it is adopted in place by
writing the settings file next to the batch folders; nothing is moved, renamed or rewritten.

Per-user state lives in the config folder (env POLYBENCH_CONFIG_DIR, default ~/.config/polybench): `recent.json` (recent
projects, newest first) and `ecad_requests.jsonl` (the "+ Request another" ECAD platform wishes; local only, nothing is sent).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import shutil
import tempfile
import time
from pathlib import Path

from . import platform as _platform

SETTINGS_NAME = "polybench-project.json"
SCHEMA = 1
PLATFORMS = [{"id": "fusion", "label": "Fusion"}, {"id": "kicad", "label": "KiCad"}]
PLATFORM_IDS = {p["id"] for p in PLATFORMS}
RECENT_MAX = 12


class SettingsUnreadable(Exception):
    """polybench-project.json exists but cannot be read or is not a settings object. Nothing may write over it."""

    def __init__(self, path, error, backup=None):
        self.path, self.error, self.backup = str(path), str(error), backup
        super().__init__(f"settings file unreadable: {self.path}, {self.error}"
                         + (f" (a copy was kept: {backup})" if backup else ""))


def _safe(fn, default=False):
    """A filesystem predicate that never raises (PermissionError, a vanished path, a loop)."""
    try:
        return fn()
    except OSError:
        return default


def is_dir(p) -> bool:
    return _safe(lambda: Path(p).is_dir())


def is_file(p) -> bool:
    return _safe(lambda: Path(p).is_file())


def readable_dir(p) -> bool:
    return is_dir(p) and os.access(p, os.R_OK | os.X_OK)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def config_dir() -> Path:
    return _platform.config_dir()


def demo_dir() -> Path:
    """The demo project's own folder (platform user-data dir, env POLYBENCH_DEMO_DIR in tests). Never the user's project folder
    and never ~/polybench-data."""
    env = os.environ.get("POLYBENCH_DEMO_DIR")
    if env:
        return Path(env).expanduser()
    return _platform.user_data_dir() / "demo-project"


def atomic_write_json(path: Path, data) -> None:
    """Write next to the target, fsync, rename: a reader sees the old file or the new one, never half of one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        _platform.replace_file(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def batch_folders(d: Path) -> list[dict]:
    """Sub-folders of `d` that hold a journal.jsonl: the batches of a project folder, newest journal first."""
    out = []
    try:
        subs = [p for p in Path(d).iterdir() if is_dir(p) and not p.name.startswith(".")]
    except OSError:
        return out
    for p in subs:
        j = p / "journal.jsonl"
        if is_file(j):
            try:
                st = j.stat()
                with open(j, "rb") as f:
                    n = sum(1 for line in f if line.strip())
            except OSError:
                continue
            out.append({"name": p.name, "dir": str(p), "journal": str(j), "events": n,
                        "modified": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)), "_mtime": st.st_mtime})
    out.sort(key=lambda x: -x["_mtime"])
    for x in out:
        x.pop("_mtime")
    return out


def read_settings(d: Path) -> dict | None:
    """The settings of a project folder; None when there is no settings file. A file that exists but cannot be read raises
    SettingsUnreadable (never treated as 'no file': the next write would replace it with a stub)."""
    f = Path(d) / SETTINGS_NAME
    try:
        present = f.exists()
    except OSError as e:
        raise SettingsUnreadable(f, e)
    if not present:
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SettingsUnreadable(f, e)
    if not isinstance(data, dict):
        raise SettingsUnreadable(f, "not a JSON object")
    return data


def backup_corrupt(f: Path) -> str | None:
    """Keep a byte copy of an unreadable settings file next to it: polybench-project.json.corrupt-<timestamp>."""
    f = Path(f)
    try:
        digest = hashlib.sha256(f.read_bytes()).digest()
        for old in f.parent.glob(f.name + ".corrupt-*"):       # one backup per distinct corrupt content, not per open attempt
            if hashlib.sha256(old.read_bytes()).digest() == digest:
                return str(old)
    except OSError:
        pass
    base = f.with_name(f"{f.name}.corrupt-{time.strftime('%Y%m%dT%H%M%S')}")
    dst, n = base, 1
    while dst.exists():
        n += 1
        dst = base.with_name(f"{base.name}-{n}")
    try:
        shutil.copy2(f, dst)
    except OSError:
        return None
    return str(dst)


def clean_stale_tmp(d: Path, min_age: float = 30.0) -> list[str]:
    """Remove `.polybench-project.json.*.tmp` left by a write that was killed. Younger files may belong to a live writer."""
    gone = []
    try:
        for x in Path(d).iterdir():
            if x.name.startswith("." + SETTINGS_NAME + ".") and x.name.endswith(".tmp"):
                try:
                    if time.time() - x.stat().st_mtime >= min_age:
                        x.unlink()
                        gone.append(str(x))
                except OSError:
                    continue
    except OSError:
        pass
    return gone


def design_of_batch(journal: Path) -> str | None:
    """The design name a batch journal recorded (batch_created.design, else plan_loaded.design); None when it names none."""
    try:
        with open(journal, "rb") as fh:
            for i, line in enumerate(fh):
                if i > 400:
                    break
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") in ("batch_created", "plan_loaded"):
                    d = (ev.get("payload") or {}).get("design")
                    if d:
                        return str(d)
    except OSError:
        return None
    return None


def _writable(d: Path) -> bool:
    return os.access(d, os.W_OK | os.X_OK)


def _warnings(p: Path, kind: str, batches: list) -> list[dict]:
    """Folders that are legal for a new project but probably a mistake: the user must confirm (Create project, D8)."""
    w = []
    if kind == "batch":
        w.append({"code": "batch_folder", "text": "This is a batch folder (it holds a journal). A project inside it would be mixed with that batch's files."})
    elif any(is_file(a / "journal.jsonl") for a in list(p.parents)[:4]):
        w.append({"code": "inside_batch", "text": "This folder is inside a batch folder. The project would be mixed with that batch's files."})
    try:
        if p == Path.home().resolve():
            w.append({"code": "home", "text": "This is your home folder. A project folder should be a folder of its own."})
    except (OSError, RuntimeError):
        pass
    try:
        kids = [c for c in p.iterdir() if is_dir(c) and is_file(c / SETTINGS_NAME)]
    except OSError:
        kids = []
    if kids:
        w.append({"code": "parent_of_project", "text": f"This folder contains the project {kids[0].name}. A new project here would sit beside it as a second one."})
    return w


def inspect(path: str | os.PathLike) -> dict:
    """What a folder is, for the New / Open forms. kind: project | legacy | batch | empty | other | missing | file | corrupt | unreadable.
    Never raises: a folder that cannot be read (permissions) is reported, not a 500."""
    p = Path(str(path or "")).expanduser()
    if not str(path or "").strip():
        return {"path": "", "kind": "missing", "exists": False, "writable": False, "creatable": False, "problem": "no folder given", "warnings": []}
    try:
        p = p.resolve()
    except (OSError, RuntimeError, ValueError):          # ValueError: a NUL byte in the path
        pass
    out: dict = {"path": str(p), "exists": _safe(p.exists), "name": p.name, "batches": [], "settings": None, "problem": None, "warnings": []}
    if not out["exists"]:
        parent = next((a for a in p.parents if _safe(a.exists)), None)
        out.update(kind="missing", writable=False, creatable=bool(parent and is_dir(parent) and _writable(parent)))
        if not out["creatable"]:
            out["problem"] = f"cannot create it: {parent} is not writable" if parent else "no existing parent folder"
        if parent is not None and is_dir(parent):
            # a path that does not exist yet is judged by its nearest existing ancestor (D8): the new folder would be created there.
            # A sibling of a project is normal (no parent_of_project warning); the ancestor being a project is a nested project.
            at = f"The nearest existing folder ({parent.name or parent}) "
            w = [{"code": x["code"], "text": at + x["text"].replace("This folder ", "", 1).replace("This ", "", 1)}
                 for x in _warnings(parent, "batch" if is_file(parent / "journal.jsonl") else "other", [])
                 if x["code"] != "parent_of_project"]
            if is_file(parent / SETTINGS_NAME):
                w.append({"code": "inside_project", "text": at + "is a project folder. A project created inside it would be nested in that project."})
            out["warnings"] = w
        return out
    if not is_dir(p):
        out.update(kind="file", writable=False, creatable=False, problem="this is a file, not a folder")
        return out
    out["writable"] = _writable(p)
    out["creatable"] = True
    if not readable_dir(p):
        out.update(kind="unreadable", writable=False, creatable=False, problem="this folder cannot be read (permission denied)")
        return out
    try:
        s = read_settings(p)
    except SettingsUnreadable as e:
        out.update(kind="corrupt", problem=str(e), settings_error=e.error)
        return out
    batches = batch_folders(p)
    out["batches"] = batches
    if s is not None:
        out.update(kind="project", settings=s, name=s.get("name") or p.name)
    elif is_file(p / "journal.jsonl"):
        out.update(kind="batch", problem="this is a batch folder; open its parent folder (the project)")
        out["parent"] = str(p.parent)
    elif batches:
        out.update(kind="legacy")
    else:
        try:
            empty = not any(x for x in p.iterdir() if not x.name.startswith("."))
        except OSError:
            empty = False
        out.update(kind="empty" if empty else "other")
    out["warnings"] = _warnings(p, out["kind"], batches)
    if not out["writable"] and not out["problem"]:
        out["problem"] = "the folder is not writable"
    return out


class Workspace:
    """The open project folder and its settings. `save()` writes the settings file atomically; nothing else writes it."""

    def __init__(self, project_dir: Path, settings: dict | None = None, persist: bool = True, adopted: bool = False):
        self.dir = Path(project_dir).expanduser().resolve()
        self.persist = persist
        self.adopted = adopted
        self.fallback: dict = {}             # values a write may fill in when the file lacks them (protocol_path, current_batch)
        if settings is None:
            try:
                settings = read_settings(self.dir) or {}
            except SettingsUnreadable as e:
                raise SettingsUnreadable(e.path, e.error, backup_corrupt(Path(e.path)))
        self.settings = settings
        if persist:
            clean_stale_tmp(self.dir)
        self.overrides: dict = {}            # CLI flags that override a setting for this run only: {field: value}

    # ---- settings
    @property
    def file(self) -> Path:
        return self.dir / SETTINGS_NAME

    @property
    def exists(self) -> bool:
        return self.file.is_file()

    @property
    def name(self) -> str:
        return self.settings.get("name") or self.dir.name

    def get(self, key, default=None):
        return self.settings.get(key, default)

    def ecad_program(self) -> str | None:
        return (self.settings.get("ecad") or {}).get("program")

    def save(self, **changes) -> dict:
        """Apply changes, stamp `updated`, write atomically. Returns {field: [old, new]} of what changed."""
        diff = {}
        for k, v in changes.items():
            old = self.settings.get(k)
            if old != v:
                diff[k] = [old, v]
            self.settings[k] = v
        self._complete()
        self.settings["updated"] = now()
        if self.persist:
            atomic_write_json(self.file, self.settings)
        return diff

    def _complete(self) -> None:
        """Every write carries every field of the schema, so a file that was valid JSON but short is never rewritten half-empty."""
        st = self.settings
        st.setdefault("schema", SCHEMA)
        st.setdefault("name", self.dir.name)
        st.setdefault("created", now())
        st.setdefault("updated", now())
        for k in ("protocol_path", "current_batch"):
            if not st.get(k) and self.fallback.get(k):
                st[k] = self.fallback[k]
            st.setdefault(k, None)
        st.setdefault("bom_path", None)
        e = st.get("ecad")
        e = dict(e) if isinstance(e, dict) else {}
        e.setdefault("program", None)
        e.setdefault("document", None)
        e.setdefault("documents", {})
        st["ecad"] = e

    def documents(self) -> dict:
        """The confirmed ECAD document of each platform: {program: {name, id, fingerprint, elements, editor, accepted?}}."""
        d = (self.settings.get("ecad") or {}).get("documents")
        return dict(d) if isinstance(d, dict) else {}

    def set_ecad(self, program: str | None = None, document: dict | None | str = "keep", documents: dict | str = "keep") -> dict:
        e = dict(self.settings.get("ecad") or {})
        if program is not None:
            e["program"] = program
        if document != "keep":
            e["document"] = document
        if documents != "keep":
            e["documents"] = documents
        return self.save(ecad=e)

    @classmethod
    def create(cls, project_dir: Path, name: str, protocol: str, ecad: str | None, batch: str) -> "Workspace":
        ws = cls(project_dir, settings={}, persist=True)
        ws.dir.mkdir(parents=True, exist_ok=True)
        ws.settings = {"schema": SCHEMA, "name": name, "created": now(), "updated": now(), "protocol_path": str(protocol),
                       "bom_path": None, "ecad": {"program": ecad, "document": None, "documents": {}}, "current_batch": batch}
        atomic_write_json(ws.file, ws.settings)
        return ws

    def info(self) -> dict:
        return {"dir": str(self.dir), "name": self.name, "settings_file": str(self.file), "settings_exists": self.exists,
                "settings": self.settings, "adopted": self.adopted or bool(self.settings.get("adopted")), "overrides": dict(self.overrides)}


# ---- recent projects (newest first)
def recent_file() -> Path:
    return config_dir() / "recent.json"


def recent_list() -> list[dict]:
    try:
        data = json.loads(recent_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("projects") if isinstance(data, dict) else None
    return [x for x in (items or []) if isinstance(x, dict) and x.get("dir")]


def recent_touch(project_dir: Path, name: str, batch: str) -> None:
    d = str(Path(project_dir).expanduser().resolve())
    items = [x for x in recent_list() if x.get("dir") != d]
    items.insert(0, {"dir": d, "name": name, "batch": batch, "opened": now()})
    atomic_write_json(recent_file(), {"schema": 1, "projects": items[:RECENT_MAX]})


def recent_marked() -> list[dict]:
    """The recent list with `missing: true` on entries whose folder is gone (the UI offers Remove from list)."""
    return [{**x, "missing": not is_dir(Path(x["dir"]))} for x in recent_list()]


def recent_remove(d: str) -> None:
    items = [x for x in recent_list() if x.get("dir") != d]
    atomic_write_json(recent_file(), {"schema": 1, "projects": items})


def recent_last() -> dict | None:
    for x in recent_list():
        if is_file(Path(x["dir"]) / SETTINGS_NAME):
            return x
    return None


# ---- "+ Request another" ECAD platform (local file only)
def ecad_requests_file() -> Path:
    return config_dir() / "ecad_requests.jsonl"


def ecad_request_add(text: str, project: str = "") -> dict:
    text = " ".join(str(text or "").split())[:200]
    if not text:
        raise ValueError("say which ECAD program you would like")
    f = ecad_requests_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": now(), "text": text, "project": project}
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return {"ok": True, "file": str(f), "request": rec}


def ecad_requests() -> list[dict]:
    try:
        lines = ecad_requests_file().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out[-10:]


# ---- server-side folder / file browser (a browser page cannot pick server paths)
def browse(path: str | None, mode: str = "dir", exts: list[str] | None = None, extra_roots: list[tuple[str, str]] | None = None,
           bom_only: bool = False) -> dict:
    """List a folder for the in-page browser. Hidden entries are left out; files are listed only in mode 'file' (filtered by
    extension; with bom_only, only files that can be a BOM: .csv, .xlsx, and .yaml/.yml with a top-level `bom:` key). Each folder
    entry says whether it is a project, a batch folder, or unreadable (permission denied). Never raises."""
    home = Path.home()
    p = Path(path).expanduser() if path else home
    try:
        p = p.resolve()
    except (OSError, RuntimeError, ValueError):          # ValueError: a NUL byte in the path
        pass
    while not is_dir(p) and p != p.parent:
        p = p.parent
    exts = [e.lower() if e.startswith(".") else "." + e.lower() for e in (exts or [])]
    entries, err, hidden_n = [], None, 0
    try:
        items = sorted(p.iterdir(), key=lambda x: x.name.lower())
    except OSError as e:
        items, err = [], f"cannot read this folder: {e.strerror or e}"
    for x in items:
        if x.name.startswith("."):
            continue
        if is_dir(x):
            ok = readable_dir(x)
            kind = None
            if ok:
                kind = "project" if is_file(x / SETTINGS_NAME) else "batch" if is_file(x / "journal.jsonl") else None
            entries.append({"name": x.name, "path": str(x), "dir": True, "kind": kind, "unreadable": not ok})
        elif mode == "file" and (not exts or any(x.name.lower().endswith(e) for e in exts)):
            if bom_only and not _bom_capable(x):
                hidden_n += 1
                continue
            try:
                size = x.stat().st_size
            except OSError:
                size = None
            entries.append({"name": x.name, "path": str(x), "dir": False, "size": size, "unreadable": not _safe(lambda: os.access(x, os.R_OK))})
    here = inspect(p) if mode == "dir" and is_dir(p) else None
    roots = [("Home", str(home))] + [(n, str(home / n)) for n in ("Desktop", "Documents", "Downloads") if is_dir(home / n)]
    for lab, rp in extra_roots or []:
        if rp and is_dir(rp):
            roots.append((lab, rp))
    return {"path": str(p), "parent": str(p.parent) if p != p.parent else None, "entries": entries[:500], "truncated": len(entries) > 500,
            "error": err, "here": here, "hidden_files": hidden_n, "roots": [{"label": a, "path": b} for a, b in roots]}


def _bom_capable(f: Path) -> bool:
    """.csv and .xlsx are offered as they are; a yaml file only when it has a top-level `bom:` key (protocol fragments are not BOMs)."""
    n = f.name.lower()
    if n.endswith((".csv", ".xlsx")):
        return True
    if not n.endswith((".yaml", ".yml")):
        return False
    try:
        with open(f, "rb") as fh:
            head = fh.read(2_000_000)
    except OSError:
        return False
    import re
    return re.search(rb"(?m)^bom\s*:", head) is not None


def locate(name: str, size: int | None, roots: list[str]) -> list[str]:
    """A file dropped on the page gives its name and size, not its path. Look for it (one level deep) in the likely folders."""
    name = Path(str(name or "")).name
    if not name:
        return []
    found = []
    seen = set()
    for r in roots:
        if not r:
            continue
        base = Path(r).expanduser()
        try:
            subs = [c / name for c in base.iterdir() if is_dir(c) and not c.name.startswith(".")] if is_dir(base) else []
        except OSError:
            subs = []
        for c in [base / name] + subs:
            try:
                if c.is_file() and (size is None or c.stat().st_size == int(size)):
                    rp = str(c.resolve())
                    if rp not in seen:
                        seen.add(rp)
                        found.append(rp)
            except (OSError, RuntimeError, ValueError):
                continue
    return found[:10]
