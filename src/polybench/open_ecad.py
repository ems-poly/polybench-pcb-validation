"""Start from an ECAD project that is already open (`polybench --from-open-ecad`).

Asks each registered ECAD bridge what it has open, read only (status, documents, designators; nothing is selected, opened or
changed). Exactly one open document: a project is created that records that document and, when one is unambiguous, a BOM
file. None or several: it says so and lists what it found; it never picks one. Pure of the server: the CLI wires it in.

What a bridge can report today:
  kicad   the open board (file name, project name and folder), the open schematic file names, the part count and references.
          The schematic path is the project folder plus the schematic's file name, and it is checked to exist.
  fusion  the open document's name and id, the active editor and the part count. No file path (the design lives in the cloud).
  altium  nothing (interface only).
No bridge reports a BOM. The BOM link is therefore a file beside the KiCad project (one .csv/.xlsx whose name contains "bom"),
used only when exactly one such file reads as a BOM; otherwise the document's own part list is the only link and the report says so.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Found:
    bridge: str
    program: str
    document: str
    document_id: str | None = None
    editor: str | None = None
    elements: int | None = None
    project_name: str = ""
    project_dir: str = ""
    schematic: str | None = None          # absolute path, checked to exist (KiCad only)
    schematic_names: list = field(default_factory=list)
    bom_candidates: list = field(default_factory=list)
    designators: list = field(default_factory=list)
    simulated: bool = False
    problem: str = ""                      # set when this document cannot be used as it stands (several schematics, not the active tab)
    status: dict = field(default_factory=dict)

    def line(self) -> str:
        extra = f", {self.elements} parts" if self.elements is not None else ""
        where = f" in {clean(self.project_dir)}" if self.project_dir else ""
        return f"{clean(self.program)}: {clean(self.document) or '(unnamed)'}{where} ({clean(self.editor) or 'editor unknown'}{extra})"


def clean(x) -> str:
    """One line, no control characters: names come from another program and are printed to the terminal."""
    return "".join(c if c.isprintable() else " " for c in str(x or "")).strip()


def slug(x, fallback: str = "design") -> str:
    """A safe folder-name part from ECAD text: letters, digits, dot, dash, underscore; no separators, no leading dots."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", clean(x)).strip("-._")[:60] or fallback


def pick_named(names: list, document: str | None) -> list:
    """The names an operator's --document selects: the one that matches exactly (case-insensitive), else the ones that contain it.
    More than one result means the caller must refuse and list them."""
    if not document:
        return list(names)
    d = document.lower()
    exact = [n for n in names if str(n).lower() == d]
    return exact or [n for n in names if d in str(n).lower()]


def bom_candidates(project_dir: str) -> list[str]:
    """BOM-looking files directly in the project folder: .csv/.xlsx with 'bom' in the name that bomfile can read as a BOM."""
    from . import bomfile
    d = Path(project_dir) if project_dir else None
    if not d or not d.is_dir():
        return []
    out = []
    for f in sorted(d.iterdir()):
        if f.is_file() and f.suffix.lower() in (".csv", ".xlsx") and "bom" in f.name.lower():
            try:
                bomfile.load(str(f))
            except Exception:
                continue
            out.append(str(f.resolve()))
    return out


def _one(bridge, name: str, document: str | None = None) -> tuple[list[Found], str]:
    """(found documents, a line saying why none). One Found per open document the bridge lists; never raises."""
    try:
        st = bridge.status()
    except Exception as e:  # noqa: BLE001
        return [], f"{name}: {type(e).__name__}: {clean(e)}"
    if not st.get("connected"):
        return [], f"{name}: not reachable ({clean(st.get('detail')) or 'no detail'})"
    if not st.get("document"):
        return [], f"{name}: running, but no document is open ({clean(st.get('detail'))})".strip()
    f = Found(bridge=name, program=str(st.get("program") or name), document=str(st["document"]), document_id=st.get("document_id"),
              editor=st.get("editor"), elements=st.get("elements"), simulated=bool(st.get("simulated")), status=dict(st))
    proj = st.get("project")
    if isinstance(proj, dict):
        f.project_name, f.project_dir = str(proj.get("name") or ""), str(proj.get("path") or "")
    opens, docs = [], {}
    try:
        docs = bridge.documents() if hasattr(bridge, "documents") else {}
        f.schematic_names = [s for s in (docs.get("schematic_open") or []) if s]
        opens = [o for o in (docs.get("open") or []) if o.get("name")]
    except Exception:  # noqa: BLE001
        pass
    try:
        f.designators = [str(x) for x in (bridge.designators() or [])]
    except Exception:  # noqa: BLE001
        f.designators = []
    if f.project_dir and not f.simulated:
        f.bom_candidates = bom_candidates(f.project_dir)
        if len(f.schematic_names) > 1:
            pick = pick_named(f.schematic_names, document) if document else []
            if len(pick) == 1:
                f.schematic_names = pick
            else:
                f.problem = (f"{len(f.schematic_names)} schematics are open ({', '.join(clean(s) for s in f.schematic_names)}); "
                             "name one with --document")
        if len(f.schematic_names) == 1:                   # only what the program named; a file with the board's stem is not assumed
            p = Path(f.project_dir) / f.schematic_names[0]
            if p.is_file():
                f.schematic = str(p.resolve())
    if len(opens) > 1:
        out = []
        chosen = set(pick_named([str(o["name"]) for o in opens], document))
        for o in opens:
            if str(o["name"]) not in chosen:
                continue
            g = Found(**{**f.__dict__, "document": str(o["name"]), "document_id": o.get("id"), "problem": f.problem})
            if not o.get("active"):
                g.designators, g.elements = [], None
                g.problem = "not the active tab; make it the active document in the program (only the active one can be read)"
            out.append(g)
        if not out:
            return [], f"{name}: no open document matches --document {clean(document)!r}"
        return out, ""
    return [f], ""


def probe(bridges: dict, document: str | None = None) -> tuple[list[Found], list[str]]:
    """Ask each bridge ({name: bridge}). Returns (found, notes about the ones that reported nothing).
    `document` (a name fragment) narrows several open documents or schematics to the one the operator named."""
    found, notes = [], []
    for name, br in bridges.items():
        fs, why = _one(br, name, document)
        if fs:
            found.extend(fs)
        else:
            notes.append(why)
    return found, notes


def registered_bridges(only: str | None = None, simulate: bool = False) -> dict:
    """One instance of each registered `polybench.ecad` bridge (or just `only`). With simulate, the simulated ones."""
    import importlib.metadata as md
    out = {}
    for ep in sorted(md.entry_points(group="polybench.ecad"), key=lambda e: e.name):
        if only and ep.name != only:
            continue
        try:
            cls = ep.load()
            out[ep.name] = cls(simulate=True) if simulate and ep.name in ("kicad", "fusion") else cls()
        except Exception as e:  # noqa: BLE001
            out[ep.name] = _Broken(str(e))
    return out


class _Broken:
    def __init__(self, why):
        self.why = why

    def status(self):
        return {"connected": False, "detail": f"bridge failed to load: {self.why}"}


def describe(found: list[Found], notes: list[str]) -> str:
    if len(found) == 1 and not found[0].problem:
        return "Found one open ECAD document:\n  " + found[0].line()
    if len(found) == 1:
        return f"Found one open ECAD document, but it cannot be used yet:\n  {found[0].line()}\n  {found[0].problem}"
    head = ("No ECAD program reports an open document." if not found else
            f"{len(found)} open ECAD documents were found; Polybench does not choose between them:")
    body = [f"  {i + 1}. {f.line()}" for i, f in enumerate(found)] + [f"  - {clean(n)}" for n in notes]
    tail = "\nOpen the one design in one program, or name the program with --ecad kicad|fusion, or the document with --document NAME." if found else \
        "\nOpen the board or schematic in KiCad (API server on) or Fusion (Electronics editor, MCP on), then run this again."
    return head + "\n" + "\n".join(body) + tail


def create_project(f: Found, project_dir: Path, protocol: str, batch: str | None, name: str | None = None) -> dict:
    """Create the project folder's settings from one open document. Refuses a folder that already holds a project."""
    from . import bomfile, workspace as wslib
    from .project import fingerprint
    info = wslib.inspect(str(project_dir))
    if info["kind"] in ("project", "legacy", "corrupt", "batch", "file", "unreadable"):
        raise ValueError(f"{info['path']} already holds a project or cannot be used ({info['kind']}); choose an empty folder with --data-dir")
    design = slug(f.project_name or Path(clean(f.document)).stem)
    batch = batch or f"{design}_{time.strftime('%Y-%m-%d')}"
    root = Path(project_dir).expanduser().resolve()
    if (root / batch).resolve().parent != root:
        raise ValueError(f"the batch name {clean(batch)!r} would put the batch folder outside {root}; give a plain --batch name")
    name = slug(name, "") if name else None
    ws = wslib.Workspace.create(Path(project_dir), name or design, str(Path(protocol).expanduser().resolve()), f.bridge, batch)
    doc = {"name": f.document, "id": f.document_id, "fingerprint": fingerprint(f.designators) if f.designators else None,
           "elements": len(f.designators) or f.elements, "program": f.bridge, "editor": f.editor,
           "schematic_path": f.schematic, "linked_from": "open-ecad"}
    ws.set_ecad(f.bridge, doc, {f.bridge: dict(doc)})
    bom = None
    if len(f.bom_candidates) == 1:
        bom = f.bom_candidates[0]
        bomfile.load(bom)
        ws.save(bom_path=bom)
    return {"project": str(ws.dir), "batch": batch, "ecad": f.bridge, "document": f.document, "schematic": f.schematic,
            "bom": bom, "bom_candidates": f.bom_candidates, "settings": str(ws.file)}


def check_inputs(a) -> str | None:
    """Everything that can be checked before anything is written. Returns a message, or None when the inputs are usable."""
    if not a.protocol:
        return ("A new project also needs its protocol: add --protocol path/to/design.pcbv.yaml (the test steps and limits; "
                "the BOM there is replaced by the BOM file when one is linked).")
    p = Path(a.protocol).expanduser()
    if not p.is_file():
        return f"the protocol file {p} does not exist; nothing was created"
    try:
        from .protocol import load
        pr = load(str(p))
    except Exception as e:  # noqa: BLE001
        return f"the protocol file {p} cannot be read ({type(e).__name__}); nothing was created"
    if pr.errors:
        return f"the protocol has {len(pr.errors)} error(s): " + "; ".join(str(i) for i in pr.errors[:3]) + "; nothing was created"
    return None


def record_in_journal(a, f: Found, bridge, res: dict) -> None:
    """Record the document the way design step 1 does (project_select, bom_crosscheck and project_settings journal events), so the UI
    shows it. Opens the new project's session once, only to write those events; nothing is sent to the ECAD program."""
    from . import workspace as wslib
    from .project import fingerprint
    from .server import open_workspace_session
    ws = wslib.Workspace(Path(res["project"]))
    sess = open_workspace_session(ws, res["batch"], bool(a.simulate), getattr(a, "boards", None), getattr(a, "operator", None) or "", f.bridge)
    st = {**f.status, "program": f.program, "document": f.document, "document_id": f.document_id, "editor": f.editor,
          "simulated": f.simulated}
    sess._record_document(bridge, st, f.designators, fingerprint(f.designators))
    ws.set_ecad(None, {**ws.documents().get(f.bridge, {}), "schematic_path": f.schematic, "linked_from": "open-ecad"},
                {**ws.documents(), f.bridge: {**ws.documents().get(f.bridge, {}), "schematic_path": f.schematic, "linked_from": "open-ecad"}})


def run(a, bridges: dict | None = None, out=print) -> int | None:
    """CLI step. Returns an exit code to stop with (2: nothing or several found, bad inputs, or an unusable folder) or None after
    creating the project, with a.data_dir set to it and a.protocol cleared so the normal start reopens it."""
    from .store import default_data_dir
    bad = check_inputs(a)
    if bad:
        out(bad)
        return 2
    br = bridges if bridges is not None else registered_bridges(a.ecad, bool(a.simulate))
    found, notes = probe(br, getattr(a, "document", None))
    out(describe(found, notes))
    if len(found) != 1 or found[0].problem:
        return 2
    f = found[0]
    if not f.designators:
        out("The open document reports no parts, so there is nothing to link; make the board or schematic editor active and run this again.")
        return 2
    d = Path(a.data_dir).expanduser() if a.data_dir else default_data_dir()
    try:
        res = create_project(f, d, a.protocol, a.batch)
        record_in_journal(a, f, br[f.bridge], res)
    except Exception as e:  # noqa: BLE001
        out(f"could not create the project: {clean(e)}")
        return 2
    out(f"Created project {res['project']} (batch {res['batch']}, ECAD {res['ecad']}).")
    out(f"  document: {clean(res['document'])}")
    out(f"  schematic: {res['schematic'] or 'not linked (' + ('the program names no schematic file' if not f.schematic_names else 'file not found in the project folder') + ')'}")
    if res["bom"]:
        out(f"  BOM: {res['bom']}")
    elif len(res["bom_candidates"]) > 1:
        out("  BOM: several BOM files beside the project, none linked: " + ", ".join(Path(x).name for x in res["bom_candidates"]))
    else:
        out("  BOM: no BOM file found beside the project; the protocol's BOM stays, the open document's part list is cross-checked against it.")
    a.data_dir, a.protocol = str(d), None
    return None
