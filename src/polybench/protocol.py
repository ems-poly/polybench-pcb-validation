"""Protocol loader (schema pcbv.protocol/2, DV subset): include merge and lint.

Every problem is reported with file and line. Errors stop the run, warnings do not.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SCHEMA = "pcbv.protocol/2"
STEP_KINDS = ["setup", "assembly", "visual", "unpowered", "powered", "function", "automated", "characterisation", "report"]
STEP_TITLES = {
    "setup": "Batch setup", "assembly": "Assembly aid", "visual": "Visual", "unpowered": "Unpowered testing",
    "powered": "Powered testing", "function": "Program and function", "automated": "Automated test", "characterisation": "Characterisation",
    "report": "Report",
}
TEST_KINDS = ["number", "yn", "choice", "text", "hex", "table"]
RECORD_MODES = ["auto", "propose"]
SECTION_POSITIONS = ["before_placement", "after_placement"]      # `position:` of an Assembly aid section (UIR-21); none = after_placement
LIMIT_CLASSES = ["hard", "guess", "tbr", "record"]
GUESS_SOURCES = {"guess", "tbr", "assumption", "estimate"}
PLUGIN_RE = re.compile(r"^[A-Za-z_][\w-]*(\.[A-Za-z_][\w-]*)+$")


def design_label(d: dict) -> str:
    """`NAME \u00b7 VARIANT rev R`: the optional design.variant (a module or population option, for example N16R8) is a field of its
    own, not a part of the revision text. Name and revision only: `NAME rev R`."""
    out = str(d.get("name", ""))
    if d.get("variant"):
        out += f" \u00b7 {d['variant']}"
    if d.get("revision") not in (None, ""):
        out += f" rev {d['revision']}"
    return out


class LDict(dict):
    line = 0
    file = ""

    def kline(self, key):
        return getattr(self, "klines", {}).get(key, self.line)


class LList(list):
    line = 0


class _Loader(yaml.SafeLoader):
    pass


def _construct_mapping(loader, node, deep=False):
    d = LDict()
    d.line = node.start_mark.line + 1
    d.klines = {}
    for knode, vnode in node.value:
        key = loader.construct_object(knode, deep=True)
        d[key] = loader.construct_object(vnode, deep=True)
        d.klines[key] = knode.start_mark.line + 1
    return d


def _construct_seq(loader, node, deep=False):
    lst = LList(loader.construct_object(n, deep=True) for n in node.value)
    lst.line = node.start_mark.line + 1
    return lst


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)
_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_SEQUENCE_TAG, _construct_seq)


@dataclass
class Issue:
    severity: str  # error | warning
    file: str
    line: int
    msg: str

    def __str__(self):
        return f"{self.file}:{self.line}: {self.severity}: {self.msg}"

    def as_dict(self):
        return {"severity": self.severity, "file": self.file, "line": self.line, "msg": self.msg}


@dataclass
class Protocol:
    path: Path
    data: dict
    issues: list[Issue] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)
    hash: str = ""

    @property
    def errors(self):
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self):
        return [i for i in self.issues if i.severity == "warning"]

    # indexes -------------------------------------------------------------
    def steps(self):
        return self.data["steps"]

    def step(self, sid):
        return next((s for s in self.data["steps"] if s["id"] == sid), None)

    def tests(self):
        for s in self.data["steps"]:
            for sec in s.get("sections", []):
                for t in sec.get("tests", []):
                    yield s, sec, t

    def test(self, tid):
        for s, sec, t in self.tests():
            if t["id"] == tid:
                return s, sec, t
        return None


def known_plugins() -> set[str]:
    from . import registry
    return registry.action_names()


def _plain(x):
    """Strip the line-number subclasses so the data is JSON/yaml clean."""
    if isinstance(x, dict):
        return {k: _plain(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_plain(v) for v in x]
    return x


def _load_yaml(path: Path, issues: list[Issue]):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        issues.append(Issue("error", str(path), 0, f"cannot read file: {e}"))
        return None
    try:
        doc = yaml.load(text, Loader=_Loader)  # noqa: S506 (SafeLoader subclass)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        issues.append(Issue("error", str(path), (mark.line + 1) if mark else 0, f"YAML syntax: {getattr(e, 'problem', e)}"))
        return None
    if doc is None:
        doc = LDict()
    if not isinstance(doc, dict):
        issues.append(Issue("error", str(path), 1, "file must hold a mapping at top level"))
        return None
    _tag(doc, str(path))
    return doc


def _tag(x, fname):
    if isinstance(x, LDict):
        x.file = fname
        for v in x.values():
            _tag(v, fname)
    elif isinstance(x, list):
        for v in x:
            _tag(v, fname)


def _inject_aoi(step_list):
    """With an `aoi:` block the Visual step gets an AOI section (run and Label Studio export buttons). No tests in it:
    AOI never produces a result by itself, the operator's decisions do."""
    for s in step_list:
        if s.get("kind") == "visual" and not any(sec["id"] == "aoi" for sec in s["sections"]):
            s["sections"].insert(0, {"id": "aoi", "title": "Automated optical inspection (AOI)", "tests": [], "actions": [
                {"id": "aoi_run", "title": "Run AOI on all photos", "plugin": "aoi.run", "params": {}, "proposes": []},
                {"id": "aoi_ls", "title": "Label Studio export (advanced)", "plugin": "aoi.ls_export", "params": {}, "proposes": [],
                 "advanced": True}], "instructions": "Upload the board photos in the AOI view, run AOI, then work through the issue list."})
            return


def _test_plan(top, path: Path, issues: list[Issue]) -> dict | None:
    """Optional `test_plan:` and `test_plan_files: [<md path>]`, relative to the top protocol file. `test_plan` is a path, or a mapping
    `{file: <md path>, generate: true}`: with generate the tests of the tier steps are read from the plan (planfmt.py, docs/TEST_PLAN_FORMAT.md).
    A missing file is a warning for display only; with generate it is an error."""
    names: list[str] = []
    generate = False
    tp = top.get("test_plan")
    if tp is not None:
        if isinstance(tp, str) and tp.strip():
            names.append(tp.strip())
        elif isinstance(tp, dict) and isinstance(tp.get("file"), str) and tp["file"].strip():
            names.append(tp["file"].strip())
            generate = bool(tp.get("generate", False))
            for k in tp:
                if k not in ("file", "generate"):
                    issues.append(Issue("error", str(path), top.kline("test_plan"), f"test_plan: unknown key '{k}' (file, generate)"))
        else:
            issues.append(Issue("error", str(path), top.kline("test_plan"), "test_plan must be a path to a markdown file, or {file: <path>, generate: true}"))
    if top.get("test_plan_files") is not None:
        v = top["test_plan_files"]
        if isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v):
            names += [x.strip() for x in v]
        else:
            issues.append(Issue("error", str(path), top.kline("test_plan_files"), "test_plan_files must be a list of paths"))
    if not names:
        return None
    files = []
    for nm in names:
        f = (path.parent / nm).resolve()
        if not f.is_file():
            line = top.kline("test_plan") if nm == names[0] and tp is not None else top.kline("test_plan_files")
            issues.append(Issue("error" if (generate and nm == names[0]) else "warning", str(path), line, f"test plan file not found: {f}"))
        files.append({"name": f.name, "path": str(f), "declared": nm, "exists": f.is_file()})
    return {"files": files, "generate": generate}


def _apply_plan(top, path: Path, steps: dict, order: list, bom: list, issues: list[Issue], tp: dict):
    """test_plan.generate: replace the sections of the steps the plan defines by the sections generated from the plan.
    Hand-written tests in those steps are dropped (one warning per step); hand-written actions stay available as the action
    library: a group's `Actions:` line picks them by id."""
    from . import planfmt
    f = Path(tp["files"][0]["path"])
    lib: dict = {}
    for s in steps.values():
        for sec in s["sections"]:
            for a in sec.get("actions", []):
                if isinstance(a, dict) and "id" in a:
                    lib.setdefault(a["id"], a)
    plan = planfmt.parse(f, bom, set(lib))
    issues.extend(plan.issues)
    summary = {"file": str(f), "name": f.name, "hash": plan.hash, "errors": len(plan.errors), "warnings": len(plan.warnings),
               "format": plan.props.get("plan_format"), "tests": 0, "generated": False}
    if plan.errors:
        return summary, [], plan
    gen = planfmt.generate(plan, lib)
    for g in gen["steps"]:
        sid = g["id"]
        if sid not in steps:
            steps[sid] = {"id": sid, "sections": []}
            order.append(sid)
        dst = steps[sid]
        new_ids = {t["id"] for sec in g["sections"] for t in sec["tests"]}
        dropped = [t["id"] for sec in dst["sections"] for t in sec.get("tests", []) if isinstance(t, dict) and t.get("id") not in new_ids]
        if dropped:       # a hand-written test the plan does not have: its ID is retired for this batch, results stay in the journal
            issues.append(Issue("warning", str(path), top.kline("test_plan"),
                                f"step '{sid}': {len(dropped)} hand-written test(s) are not in {f.name} and are dropped: {', '.join(dropped[:12])}{' ...' if len(dropped) > 12 else ''}"))
        dst["sections"] = []
        for sec in g["sections"]:
            sec["_file"], sec["_line"] = str(f), sec.get("plan_line", 0)
            dst["sections"].append(sec)
        for k in ("advance", "plan_generated"):
            dst[k] = g[k]
    summary.update(generated=True, tests=sum(len(sec["tests"]) for g in gen["steps"] for sec in g["sections"]))
    return summary, gen["tier_gates"], plan


def load(path, check_plugins=True, plan_generate: bool | None = None, plan_override=None) -> Protocol:
    """`plan_override`: read the test plan from this file instead of the one the protocol names (validating a draft or a backup)."""
    path = Path(path).expanduser().resolve()
    issues: list[Issue] = []
    files: list[Path] = []
    top = _load_yaml(path, issues)
    proto = Protocol(path=path, data={"steps": [], "bom": [], "capabilities": []}, issues=issues, files=files)
    if top is None:
        return proto
    files.append(path)
    if top.get("schema") != SCHEMA:
        issues.append(Issue("error", str(path), top.kline("schema") if "schema" in top else 1,
                            f"schema must be '{SCHEMA}', got {top.get('schema')!r}"))
    design = top.get("design")
    if not isinstance(design, dict) or not design.get("name"):
        issues.append(Issue("error", str(path), top.kline("design"), "design.name is required"))
        design = {"name": "UNNAMED"}
    batch = top.get("batch") or {}
    docs = [top]
    seen = {path}

    def walk(doc, base: Path):
        for inc in doc.get("include") or []:
            p = (base.parent / str(inc)).resolve()
            line = doc.kline("include")
            if p in seen:
                issues.append(Issue("error", str(doc.file), line, f"include cycle or repeated include: {inc}"))
                continue
            seen.add(p)
            sub = _load_yaml(p, issues)
            if sub is None:
                issues.append(Issue("error", str(doc.file), line, f"include not loadable: {inc}"))
                continue
            files.append(p)
            docs.append(sub)
            walk(sub, p)

    walk(top, path)

    steps: dict[str, dict] = {}
    order: list[str] = []
    bom: list[dict] = []
    caps: dict[str, dict] = {}
    aoi: dict = {}
    for doc in docs:
        if isinstance(doc.get("aoi"), dict):       # AOI configuration; relative paths are resolved against the file that holds it
            aoi.update({k: v for k, v in doc["aoi"].items()})
            aoi["_dir"] = str(Path(str(doc.file)).resolve().parent)
        for s in doc.get("steps") or []:
            if not isinstance(s, dict) or "id" not in s:
                issues.append(Issue("error", doc.file, getattr(s, "line", 0) or doc.kline("steps"), "step without id"))
                continue
            sid = s["id"]
            if sid not in steps:
                steps[sid] = {"id": sid, "sections": []}
                order.append(sid)
            dst = steps[sid]
            for k, v in s.items():
                if k == "sections":
                    continue
                dst[k] = v
            dst.setdefault("_file", doc.file)
            dst.setdefault("_line", s.line)
            for sec in s.get("sections") or []:
                if not isinstance(sec, dict) or "id" not in sec:
                    issues.append(Issue("error", doc.file, getattr(sec, "line", s.line), f"section without id in step {sid}"))
                    continue
                cur = next((x for x in dst["sections"] if x["id"] == sec["id"]), None)
                if cur is None:
                    cur = {"id": sec["id"], "tests": [], "actions": [], "_file": doc.file, "_line": sec.line}
                    dst["sections"].append(cur)
                for k, v in sec.items():
                    if k in ("tests", "actions"):
                        cur[k].extend(v or [])
                    else:
                        cur[k] = v
        for b in doc.get("bom") or []:
            if isinstance(b, dict):
                b["_file"], b["_line"] = doc.file, b.line
            bom.append(b)
        for c in doc.get("capabilities") or []:
            if isinstance(c, dict) and "name" in c:
                caps[c["name"]] = c

    tp = _test_plan(top, path, issues)
    if tp and plan_override is not None:
        ov = Path(plan_override).resolve()
        tp["files"][0].update(path=str(ov), exists=ov.is_file())
        if ov.is_file():
            issues[:] = [i for i in issues if "test plan file not found" not in i.msg]
    plan_info, tier_gates = None, []
    if tp and tp.get("generate") and plan_generate is not False and tp["files"][0]["exists"]:
        plan_info, tier_gates, _plan = _apply_plan(top, path, steps, order, bom, issues, tp)
        files.append(Path(tp["files"][0]["path"]))
    step_list = [steps[i] for i in order]
    for kind in ("setup", "report"):  # always present: batch setup first, report last
        if not any(x.get("kind", x["id"]) == kind for x in step_list):
            step_list.insert(0, {"id": kind, "sections": []}) if kind == "setup" else step_list.append({"id": kind, "sections": []})
    for s in step_list:
        s.setdefault("kind", s["id"])
        s.setdefault("title", STEP_TITLES.get(s["kind"], s["id"]))
        s.setdefault("applies_to", "all")
        if s["kind"] == "automated":
            _normalise_automated(s)
    if aoi:
        _inject_aoi(step_list)
    data = {
        "schema": SCHEMA,
        "design": dict(design),
        "batch": dict(batch),
        "steps": step_list,
        "bom": bom,
        "capabilities": list(caps.values()),
    }
    if aoi:
        data["aoi"] = aoi
    if tp:
        data["test_plan"] = tp
    if plan_info:
        data["plan"] = plan_info
        data["tier_gates"] = tier_gates
    proto.data = data
    _lint(proto, check_plugins)
    proto.data = _plain(proto.data)
    h = hashlib.sha256()
    plan_file = Path(proto.data["plan"]["file"]) if proto.data.get("plan") else None
    for f in files:
        if plan_file is not None and f == plan_file:
            continue             # the plan has its own hash (plan_loaded event); a plan edit is not a protocol-file change
        h.update(f.read_bytes())
    proto.hash = h.hexdigest()[:16]
    return proto


SEQ_SECTION = "_sequence"
IMAGE_EXTS = (".hex", ".bin", ".elf", "")


def _normalise_automated(s: dict) -> None:
    """kind: automated. The step-level `actions:` become one hidden section `_sequence` (so every existing consumer of
    section actions, the runner, safe state, doctor, voice, keeps working); their results default to record: propose, because the
    operator accepts the whole verdict at once. `diagnose:` sections are appended as ordinary sections flagged `diagnose: true`."""
    base = Path(str(s["_file"])).resolve().parent if s.get("_file") else None

    def rel(v):
        """A relative path in the protocol is relative to the file that holds it (an absolute path is left alone)."""
        if base is None or not isinstance(v, str) or not v or Path(v).expanduser().is_absolute() or v.startswith("~"):
            return v
        return str((base / v).resolve())

    if s.get("manifest"):
        s["manifest"] = rel(s["manifest"])
    for e in s.get("equipment") or []:
        if isinstance(e, dict) and e.get("image"):
            e["image"] = rel(e["image"])
    for im in s.get("final_images") or []:
        if isinstance(im, dict) and im.get("path"):
            im["path"] = rel(im["path"])
    acts = s.pop("actions", None) or []
    for a in acts:
        if isinstance(a, dict) and isinstance(a.get("params"), dict) and isinstance(a["params"].get("image"), str):
            a["params"]["image"] = rel(a["params"]["image"])
        if isinstance(a, dict):
            a.setdefault("record", "auto" if a.get("final") else "propose")   # the final flash runs after the accept: its reading is a result
    hidden = {"id": SEQ_SECTION, "title": "Sequence", "hidden": True, "tests": [], "actions": acts}
    s["sections"].insert(0, hidden)
    seen = {t.get("id") for sec in s["sections"] for t in sec.get("tests", []) if isinstance(t, dict)}
    for d in s.pop("diagnose", None) or []:
        if isinstance(d, dict) and "id" in d:
            d["diagnose"] = True
            d.setdefault("tests", [])
            d.setdefault("actions", [])
            keep, uses = [], []
            for t in d["tests"]:
                tid = t.get("id") if isinstance(t, dict) else None
                if tid is not None and tid in seen:      # the same test as in the run: one cell, one result; the section only points at it
                    uses.append(tid)
                else:
                    keep.append(t)
                    if tid is not None:
                        seen.add(tid)
            d["tests"] = keep
            if uses:
                d["uses"] = uses
            s["sections"].append(d)
    s.setdefault("sequence", [])
    s.setdefault("final_images", [])
    by_test: dict = {}
    for a in acts:
        if isinstance(a, dict):
            for tid in a.get("proposes") or []:
                by_test.setdefault(tid, [])
                by_test[tid] += [n for n in (a.get("needs") or []) if n not in by_test[tid]]
    for sec in s["sections"]:
        if sec["id"] == SEQ_SECTION or sec.get("diagnose"):
            continue
        for t in sec.get("tests", []):
            if isinstance(t, dict) and "needs" not in t and by_test.get(t.get("id")):
                t["needs"] = list(by_test[t["id"]])


def seq_items(s: dict) -> list[dict]:
    """The sequence as items: {kind: action, id} or {kind: check, id, test, question} (an `operator_check` entry). Bad entries are skipped."""
    out = []
    for e in s.get("sequence") or []:
        if isinstance(e, str):
            out.append({"kind": "action", "id": e})
        elif isinstance(e, dict) and isinstance(e.get("operator_check"), dict) and e["operator_check"].get("id"):
            oc = e["operator_check"]
            out.append({"kind": "check", "id": oc["id"], "test": oc.get("test"), "question": str(oc.get("question") or ""), "needs": oc.get("needs") or [],
                        "title": str(oc.get("title") or oc.get("question") or oc["id"])})
    return out


def _lint_automated(s: dict, err_s, test_ids: set) -> None:
    sid = s["id"]
    hidden = next((x for x in s["sections"] if x["id"] == SEQ_SECTION), {"actions": []})
    acts = {a["id"]: a for a in hidden.get("actions", []) if isinstance(a, dict) and "id" in a}
    raw = s.get("sequence")
    if not isinstance(raw, list) or not raw:
        err_s(f"step '{sid}': kind automated needs a non-empty sequence: [action ids]")
    items = seq_items(s)
    if isinstance(raw, list) and len(items) != len(raw):
        err_s(f"step '{sid}': a sequence entry is an action id or {{operator_check: {{id, test, question}}}}")
    seq = [i["id"] for i in items]
    all_tests = {t.get("id"): t for sec in s["sections"] for t in sec.get("tests", []) if isinstance(t, dict)}
    for it in items:
        if it["kind"] == "action" and it["id"] not in acts:
            err_s(f"step '{sid}': sequence names unknown action '{it['id']}' (it must be one of the step's actions)")
        if it["kind"] == "check":
            if it["id"] in acts:
                err_s(f"step '{sid}': operator_check id '{it['id']}' is also an action id")
            t = all_tests.get(it.get("test"))
            if t is None:
                err_s(f"step '{sid}': operator_check '{it['id']}' names unknown test '{it.get('test')}'")
            elif t.get("kind", "number") != "yn":
                err_s(f"step '{sid}': operator_check '{it['id']}': test '{it['test']}' must be kind: yn")
            if not it["question"].strip():
                err_s(f"step '{sid}': operator_check '{it['id']}' needs a question")
    if len(set(seq)) != len(seq):
        err_s(f"step '{sid}': an entry appears twice in the sequence")
    finals = [i for i in seq if i in acts and acts[i].get("final")]
    if len(finals) > 1:
        err_s(f"step '{sid}': at most one action may be final: true, found {', '.join(finals)}")
    elif finals and seq[-1] != finals[0]:
        err_s(f"step '{sid}': the final action '{finals[0]}' must be last in the sequence")
    for i, a in acts.items():
        if a.get("final") and i not in seq:
            err_s(f"step '{sid}': action '{i}' is final: true but is not in the sequence", "warning")
    imgs = s.get("final_images") or []
    if finals and not imgs and (acts[finals[0]].get("params") or {}).get("image_from") == "final_images":
        err_s(f"step '{sid}': the final action takes its image from final_images but the list is empty (the operator can still use Open image...)", "warning")
    dflt = 0
    for im in imgs:
        if not isinstance(im, dict) or not im.get("label") or not im.get("path"):
            err_s(f"step '{sid}': each final_images entry needs label and path")
            continue
        dflt += 1 if im.get("default") else 0
        pth = Path(str(im["path"])).expanduser()
        if not pth.is_file():
            err_s(f"step '{sid}': final image '{im['label']}' not found on disk: {pth}")
        elif not im.get("sha256"):
            err_s(f"step '{sid}': final image '{im['label']}' has no sha256 (add it so the flashed file can be checked)", "warning")
        if pth.suffix.lower() not in IMAGE_EXTS:
            err_s(f"step '{sid}': final image '{im['label']}' should be .hex, .bin or .elf", "warning")
    if imgs and dflt != 1:
        err_s(f"step '{sid}': mark exactly one final_images entry default: true (found {dflt})", "warning" if dflt == 0 else "error")
    eq = s.get("equipment")
    if eq is not None:
        kinds = ("openocd_probe", "pcan", "serial", "usb", "n6705b")
        if not isinstance(eq, list):
            err_s(f"step '{sid}': equipment must be a list of {{id, kind, ...}}")
            eq = []
        ids = set()
        for e in eq:
            if not isinstance(e, dict) or not e.get("id") or not e.get("kind"):
                err_s(f"step '{sid}': each equipment entry needs an id and a kind ({', '.join(kinds)}, or a plug-in's own)")
                continue
            if e["id"] in ids:
                err_s(f"step '{sid}': duplicate equipment id '{e['id']}'")
            ids.add(e["id"])
            vp = e.get("vid_pid") or (e.get("match") or {}).get("vid_pid") if isinstance(e.get("match"), (dict, type(None))) else e.get("vid_pid")
            if e["kind"] in ("openocd_probe", "usb") and vp and not re.fullmatch(r"(0x)?[0-9a-fA-F]{4}:(0x)?[0-9a-fA-F]{4}", str(vp).replace(" ", "")):
                err_s(f"step '{sid}': equipment '{e['id']}': vid_pid must look like 03eb:2141")
            if e["kind"] == "usb" and not vp:
                err_s(f"step '{sid}': equipment '{e['id']}': kind usb needs vid_pid")
            rx = e.get("match") if isinstance(e.get("match"), str) else (e.get("match") or {}).get("regex") or (e.get("match") or {}).get("pattern")
            if e["kind"] == "serial" and rx:
                try:
                    re.compile(rx)
                except re.error:
                    err_s(f"step '{sid}': equipment '{e['id']}': match is not a valid regular expression")
    eq_ids = {e["id"] for e in (s.get("equipment") or []) if isinstance(e, dict) and e.get("id")}
    for a in acts.values():
        for n_ in a.get("needs") or []:
            if n_ not in eq_ids:
                err_s(f"step '{sid}': action '{a['id']}' needs unknown equipment '{n_}'")
    for sec in s["sections"]:
        for t in sec.get("tests", []):
            for n_ in (t.get("needs") or []) if isinstance(t, dict) else []:
                if n_ not in eq_ids:
                    err_s(f"step '{sid}': test '{t.get('id')}' needs unknown equipment '{n_}'")
    man = s.get("manifest")
    if man and not Path(str(man)).expanduser().is_file():
        err_s(f"step '{sid}': manifest not found: {man}", "warning")
    proposed = {t for i in seq if i in acts for t in (acts[i].get("proposes") or [])} | {it["test"] for it in items if it["kind"] == "check"}
    for sec in s["sections"]:
        if sec.get("diagnose") or sec["id"] == SEQ_SECTION:
            continue
        for t in sec.get("tests", []):
            if isinstance(t, dict) and t.get("stop") is True and t.get("id") not in proposed:
                err_s(f"step '{sid}': test '{t.get('id')}' is stop: true but no action in the sequence proposes it")


def _lint_guide(sec: dict, err) -> None:
    """Optional section key `guide: [{action: id} | {test: id}, ...]`: the order of the voice-guided walk (docs/VOICE.md, Guided mode)."""
    g = sec.get("guide")
    if g is None:
        return
    if not isinstance(g, list) or not g:
        err(sec, "guide", f"section '{sec['id']}': guide must be a non-empty list of {{action: id}} or {{test: id}}")
        return
    acts = {a.get("id") for a in sec.get("actions", []) if isinstance(a, dict)}
    tests = {t.get("id") for t in sec.get("tests", []) if isinstance(t, dict)} | set(sec.get("uses") or [])
    for e in g:
        if not isinstance(e, dict) or len(e) != 1 or next(iter(e)) not in ("action", "test"):
            err(sec, "guide", f"section '{sec['id']}': each guide entry is {{action: id}} or {{test: id}}, got {e!r}")
        elif next(iter(e)) == "action" and e["action"] not in acts:
            err(sec, "guide", f"section '{sec['id']}': guide names unknown action '{e['action']}' (it must be an action of this section)")
        elif next(iter(e)) == "test" and e["test"] not in tests:
            err(sec, "guide", f"section '{sec['id']}': guide names unknown test '{e['test']}' (it must be a test of this section)")


def _lint(p: Protocol, check_plugins: bool):
    I = p.issues
    data = p.data

    def err(node, key, msg, sev="error"):
        f = getattr(node, "file", "") or str(p.path)
        line = node.kline(key) if isinstance(node, LDict) else 0
        I.append(Issue(sev, f, line, msg))

    step_ids = {s["id"] for s in data["steps"]}
    test_ids: dict[str, tuple] = {}
    action_ids: dict[str, tuple] = {}
    plugins = known_plugins() if check_plugins else set()
    all_tests = []
    for s in data["steps"]:
        if s["kind"] not in STEP_KINDS:
            I.append(Issue("error", s.get("_file", str(p.path)), s.get("_line", 0),
                           f"step '{s['id']}': kind must be one of {', '.join(STEP_KINDS)}"))
        if s["kind"] == "automated":
            _lint_automated(s, lambda msg, sev="error": I.append(Issue(sev, s.get("_file", str(p.path)), s.get("_line", 0), msg)), set())
        gate = s.get("gate")
        if gate:
            for r in gate.get("requires", []) or []:
                if r not in step_ids:
                    err(gate, "requires", f"step '{s['id']}': gate requires unknown step '{r}'")
        at = s.get("applies_to", "all")
        if at != "all" and not isinstance(at, list):
            I.append(Issue("error", s.get("_file", ""), s.get("_line", 0), f"step '{s['id']}': applies_to must be 'all' or a list of board labels"))
        for sec in s.get("sections", []):
            sec.setdefault("title", sec["id"])
            if sec.get("position") is not None:
                if sec["position"] not in SECTION_POSITIONS:
                    err(sec, "position", f"section '{sec['id']}': position must be one of {', '.join(SECTION_POSITIONS)}")
                elif s["kind"] != "assembly":
                    err(sec, "position", f"section '{sec['id']}': position is only used by the assembly step; it is ignored here", "warning")
            for a in sec.get("actions", []):
                if not isinstance(a, dict) or "id" not in a:
                    I.append(Issue("error", sec.get("_file", ""), sec.get("_line", 0), f"action without id in section '{sec['id']}'"))
                    continue
                a.setdefault("title", a["id"])
                a.setdefault("params", {})
                a.setdefault("proposes", [])
                a.setdefault("record", "auto")
                _norm_derive(a, err)
                if a["record"] not in RECORD_MODES:
                    err(a, "record", f"action '{a['id']}': record must be one of {', '.join(RECORD_MODES)} (auto = an instrument reading is the result; propose = the operator accepts it)")
                if a["id"] in action_ids and not (sec.get("diagnose") and action_ids[a["id"]] == s["id"]):
                    err(a, "id", f"duplicate action id '{a['id']}'")
                action_ids.setdefault(a["id"], s["id"])
                pl = a.get("plugin")
                if not pl or not PLUGIN_RE.match(str(pl)):
                    err(a, "plugin", f"action '{a['id']}': plugin name must look like 'module.action', got {pl!r}")
                elif check_plugins and pl not in plugins:
                    err(a, "plugin", f"action '{a['id']}': plugin '{pl}' is not installed here (run will fail until it is)", "warning")
            for t in sec.get("tests", []):
                if not isinstance(t, dict) or "id" not in t:
                    I.append(Issue("error", sec.get("_file", ""), sec.get("_line", 0), f"test without id in section '{sec['id']}'"))
                    continue
                t.setdefault("title", t["id"])
                t.setdefault("kind", "number")
                if t["id"] in test_ids:
                    err(t, "id", f"duplicate test id '{t['id']}' (first at {test_ids[t['id']][0]}:{test_ids[t['id']][1]})")
                test_ids[t["id"]] = (getattr(t, "file", ""), getattr(t, "line", 0))
                all_tests.append((s, sec, t))
                _lint_test(t, err)
            _lint_guide(sec, err)
            _lint_derivable(s, sec, [x for x in sec.get('tests', []) if isinstance(x, dict) and 'id' in x], err)
    for s, sec, t in all_tests:
        pass
    for s in data["steps"]:
        for sec in s.get("sections", []):
            for a in sec.get("actions", []):
                if isinstance(a, dict):
                    for tid in a.get("proposes", []) or []:
                        if tid not in test_ids:
                            err(a, "proposes", f"action '{a.get('id')}' proposes unknown test '{tid}'")
    # BOM
    seen_lines, seen_des = {}, {}
    for b in data["bom"]:
        f, ln = b.get("_file", ""), b.get("_line", 0)
        if not isinstance(b, dict) or "line" not in b:
            I.append(Issue("error", f, ln, "BOM entry without 'line'"))
            continue
        if b["line"] in seen_lines:
            I.append(Issue("error", f, ln, f"duplicate BOM line {b['line']}"))
        seen_lines[b["line"]] = 1
        ds = b.get("designators")
        if not ds or not isinstance(ds, list):
            I.append(Issue("error", f, ln, f"BOM line {b['line']}: designators must be a non-empty list"))
            continue
        for d in ds:
            if d in seen_des:
                I.append(Issue("error", f, ln, f"designator {d} appears on BOM lines {seen_des[d]} and {b['line']}"))
            seen_des[d] = b["line"]
        # optional hand-assembly hints (Assembly aid "List by: hand assembly"): absent is fine, present must be numbers
        if "height_mm" in b and not (_num(b["height_mm"]) and b["height_mm"] > 0):
            I.append(Issue("error", f, ln, f"BOM line {b['line']}: height_mm must be a positive number"))
        if "body_mm" in b and not (isinstance(b["body_mm"], list) and len(b["body_mm"]) == 2 and all(_num(v) and v > 0 for v in b["body_mm"])):
            I.append(Issue("error", f, ln, f"BOM line {b['line']}: body_mm must be [length, width] in mm"))
        b.setdefault("order", b["line"])
        b.setdefault("dnf", False)
        b.setdefault("barcode", [])
    for c in data["capabilities"]:
        c.setdefault("optional", False)
    # highlight designators not on the BOM are test points; only warn when a BOM exists and nothing matches
    if data["bom"]:
        for s, sec, t in all_tests:
            hl = t.get("highlight") or []
            if hl and not isinstance(hl, list):
                err(t, "highlight", f"test '{t['id']}': highlight must be a list of designators")


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _lint_table(t, err):
    tid = t["id"]
    cols = t.get("columns")
    if not isinstance(cols, list) or not cols or not all(isinstance(c, dict) and c.get("id") for c in cols):
        err(t, "columns", f"test '{tid}': kind table needs columns: [{{id, title, unit}}, ...]")
        return
    ids = [c["id"] for c in cols]
    if len(set(ids)) != len(ids):
        err(t, "columns", f"test '{tid}': duplicate column id")
    for cid, lim in (t.get("limits") or {}).items():
        if cid not in ids:
            err(t, "limits", f"test '{tid}': limits for unknown column '{cid}'")
            continue
        if not isinstance(lim, dict) or lim.get("class") not in LIMIT_CLASSES:
            err(t, "limits", f"test '{tid}': column '{cid}' limit needs class ({', '.join(LIMIT_CLASSES)})")
            continue
        if lim["class"] in ("hard", "guess") and not lim.get("source"):
            err(t, "limits", f"test '{tid}': column '{cid}' {lim['class']} limit without source")
        if lim["class"] in ("guess", "tbr") and not lim.get("closes_on"):
            err(t, "limits", f"test '{tid}': column '{cid}' {lim['class']} limit needs closes_on")
        for k in ("min", "max"):
            if k in lim and not _num(lim[k]):
                err(t, "limits", f"test '{tid}': column '{cid}' limit {k} must be a number")
        if lim["class"] in ("hard", "guess") and not any(k in lim for k in ("min", "max")):
            err(t, "limits", f"test '{tid}': column '{cid}' limit needs min and/or max")
    pl = t.get("plot")
    if pl is not None and (not isinstance(pl, dict) or pl.get("x") not in ids or pl.get("y") not in ids):
        err(t, "plot", f"test '{tid}': plot needs x and y, each a column id")
    si = t.get("stop_if")
    if si is not None and (not isinstance(si, dict) or si.get("column") not in ids):
        err(t, "stop_if", f"test '{tid}': a table stop_if names a column")


def _norm_derive(a, err):
    """`derive:` rules of an action (PW-6): each {test, from[, map, unit, note]}; the derived tests join the action's `proposes`."""
    d = a.get("derive")
    if d is None:
        return
    if not isinstance(d, list):
        err(a, "derive", f"action '{a.get('id')}': derive must be a list of {{test, from, map}}")
        a["derive"] = []
        return
    for r in d:
        if not isinstance(r, dict) or not r.get("test") or not r.get("from"):
            err(a, "derive", f"action '{a.get('id')}': every derive rule needs test and from (a path in the plugin's analysis, e.g. analysis.tripped)")
            continue
        if r.get("map") is not None and not isinstance(r["map"], dict):
            err(a, "derive", f"action '{a.get('id')}': derive map for {r['test']} must be a mapping such as {{true: yes, false: no}}")
        if r["test"] not in a["proposes"]:
            a["proposes"] = list(a["proposes"]) + [r["test"]]


_HUMAN_WORDS = ("smell", "smoke", "hot", "touch", "finger", "led", "colour", "color", "lead", "wired", "wiring", "connected", "nothing on", "fitted", "polarity", "visible", "flush")
_DATA_WORDS = ("limit", "trip", "protection", "current", "voltage", "rail", " v ", "volts", "within", "stayed", "under", "above", "below", "ma ")


def _lint_derivable(s, sec, tests, err):
    """Warn: a yn or number test in a section with an instrument action that nothing proposes, whose title reads like a quantity the
    instrument measured. Asking the operator is for what only a person can judge (smell, heat, colour, wiring)."""
    acts = [a for a in sec.get("actions", []) if isinstance(a, dict) and str(a.get("plugin", "")).startswith(("n6705b.", "pcan.", "meter."))]
    if not acts:
        return
    covered = {t for a in sec.get("actions", []) if isinstance(a, dict) for t in a.get("proposes") or []}
    for t in tests:
        if t.get("kind") not in ("yn", "number") or t["id"] in covered or "action" in (t.get("inputs") or []):
            continue
        title = " " + str(t.get("title", "")).lower() + " "
        if any(w in title for w in _HUMAN_WORDS) or not any(w in title for w in _DATA_WORDS):
            continue
        err(t, "inputs", f"test '{t['id']}': the section's {acts[0].get('plugin')} action collects data that may answer this; "
                         "derive it (action 'derive:' with a path in the analysis) or the operator is asked what the instrument knows", "warning")


def _lint_test(t, err):
    tid = t["id"]
    if t["kind"] not in TEST_KINDS:
        err(t, "kind", f"test '{tid}': kind must be one of {', '.join(TEST_KINDS)}")
    if t["kind"] == "choice" and not t.get("options"):
        err(t, "options", f"test '{tid}': kind choice needs options")
    if t["kind"] == "table":
        _lint_table(t, err)
        return
    lim = t.get("limit")
    if lim is None:
        return
    if not isinstance(lim, dict):
        err(t, "limit", f"test '{tid}': limit must be a mapping")
        return
    cls = lim.get("class")
    if cls is None:
        err(lim, "class", f"test '{tid}': limit without class")
        return
    if cls not in LIMIT_CLASSES:
        err(lim, "class", f"test '{tid}': limit class must be one of {', '.join(LIMIT_CLASSES)}")
        return
    src = lim.get("source")
    if cls in ("hard", "guess") and not src:
        err(lim, "class", f"test '{tid}': {cls} limit without source")
    if cls == "tbr" and not src:
        err(lim, "class", f"test '{tid}': tbr limit has no source (say where the number came from)", "warning")
    if cls == "hard" and isinstance(src, dict) and str(src.get("type", "")).lower() in GUESS_SOURCES:
        err(lim, "source", f"test '{tid}': hard limit with a guess source ({src.get('type')}); make it class guess or tbr")
    if isinstance(src, dict) and src and "type" not in src:
        err(lim, "source", f"test '{tid}': source needs a type", "warning")
    if cls in ("guess", "tbr") and not lim.get("closes_on"):
        err(lim, "class", f"test '{tid}': {cls} limit needs closes_on (what measurement or decision closes it)")
    for k in ("min", "max"):
        if k in lim and not _num(lim[k]):
            err(lim, k, f"test '{tid}': limit {k} must be a number")
    if _num(lim.get("min")) and _num(lim.get("max")) and lim["min"] > lim["max"]:
        err(lim, "min", f"test '{tid}': limit min is above max")
    if t["kind"] == "number" and cls in ("hard", "guess") and not any(k in lim for k in ("min", "max")):
        err(lim, "class", f"test '{tid}': number limit needs min and/or max")
    si = t.get("stop_if")
    if si is not None:
        if not isinstance(si, dict) or not any(k in si for k in ("below", "above", "equals", "outside")):
            err(t, "stop_if", f"test '{tid}': stop_if needs below, above, outside or equals")
        else:
            for k in ("below", "above"):
                if k in si and not _num(si[k]):
                    err(si, k, f"test '{tid}': stop_if {k} must be a number")
