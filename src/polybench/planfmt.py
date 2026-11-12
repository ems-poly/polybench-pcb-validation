"""Test plan as master (plan_format 1): parse the markdown plan, lint it with line numbers, generate protocol steps from it.

The rules are in docs/TEST_PLAN_FORMAT.md. The parser reads only what the format defines: the front matter, the `## Tier ...`
sections with their `### group` headings and one table each, the `## Decision gate` block, and the reserved `## Function`,
`## Characterisation` and `## Visual` sections. Every other heading and all prose is ignored.

Nothing here touches Fusion, the journal or the network. `parse()` never raises for a bad plan: problems are Issues with the
plan file's line numbers, the same Issue class the protocol loader uses.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .protocol import Issue
from .testplan import _cells, strip_comments

PLAN_FORMAT = 1
COLUMNS = ["ID", "Test", "Probe +", "Probe -", "Nets", "Highlight", "Kind", "Unit", "Min", "Max", "Class", "Source", "Stop if",
           "Outlier", "Inputs"]
KINDS = ["number", "yn", "choice", "text"]
CLASSES = ["hard", "guess", "tbr", "record"]
INPUTS = ["keyboard", "voice", "action"]
DEFAULT_INPUTS = ["keyboard", "voice"]
UNITS = ["Ohm", "kOhm", "MOhm", "mOhm", "V", "mV", "uV", "kV", "A", "mA", "uA", "Hz", "kHz", "MHz", "s", "ms", "us", "%", "degC",
         "ppm", "mV/V", "mm", "count", "dB"]
UNIT_ALIAS = {"Ω": "Ohm", "kΩ": "kOhm", "MΩ": "MOhm", "mΩ": "mOhm", "µV": "uV", "µA": "uA", "µs": "us", "ohm": "Ohm", "kohm": "kOhm",
              "°C": "degC"}
RESERVED_IDS = {"PASS", "FAIL", "OK", "OKAY", "NOTE", "TEXT", "DEFECT", "NEXT", "SKIP", "ACCEPT", "REJECT", "UNDO", "BACK", "REPEAT",
                "ALL", "ROW", "BOARD"}
RESERVED_SECTIONS = {"function": "Function", "characterisation": "Characterisation", "visual": "Visual"}
GUESS_SOURCE_WORDS = {"guess", "tbr", "assumption", "estimate"}
NUM = r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
EMPTY = "—"
STEP_OF = {"unpowered": "unpowered", "powered": "powered"}


@dataclass
class Test:
    line: int
    id: str
    title: str
    cells: dict
    group: "Group | None" = None
    instructions: str = ""
    options: list = field(default_factory=list)


@dataclass
class Group:
    line: int
    title: str
    designators: list
    section: "Section | None" = None
    paragraph: str = ""
    actions: list = field(default_factory=list)       # [(id, line)]
    tests: list = field(default_factory=list)
    table_line: int = 0


@dataclass
class Section:
    line: int
    title: str
    kind: str                       # tier | gate | reserved
    tier: int = 0
    mode: str = ""                  # unpowered | powered
    deeper: bool = False
    groups: list = field(default_factory=list)
    gates: list = field(default_factory=list)


@dataclass
class Plan:
    path: str
    text: str
    hash: str
    props: dict
    sections: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    gates: list = field(default_factory=list)
    bom_checked: bool = False

    @property
    def errors(self):
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self):
        return [i for i in self.issues if i.severity == "warning"]

    def tests(self):
        for s in self.sections:
            for g in s.groups:
                for t in g.tests:
                    yield s, g, t


# --------------------------------------------------------------------------- reading
def file_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _blank_comments(text: str) -> str:
    """Replace %% ... %% comments by blanks but keep every newline, so line numbers still match the file."""
    return re.sub(r"%%.*?%%", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)


def _norm_header(h: str) -> str:
    h = re.sub(r"[`*]", "", h).strip()
    h = h.replace("−", "-").replace("–", "-").replace("—", "-")
    h = re.sub(r"\s+", " ", h)
    for c in COLUMNS:
        if h.lower() == c.lower():
            return c
    return h


def _empty(v: str) -> bool:
    return v.strip() in ("", "—", "–", "-")


def _items(cell: str) -> list:
    return [] if _empty(cell) else [x.strip() for x in cell.split(",") if x.strip()]


def parse(path, bom: list | None = None, actions: set | None = None) -> Plan:
    """Parse and lint. `bom` = list of protocol BOM lines (dicts with `designators`); None skips the BOM checks.
    `actions` = ids of the actions the protocol defines; None skips that check."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        p = Plan(str(path), "", "", {})
        p.issues.append(Issue("error", str(path), 0, f"cannot read the plan: {e}"))
        return p
    plan = Plan(str(path), text, file_hash(text), {})
    I = plan.issues
    F = str(path)

    def err(line, msg, sev="error"):
        I.append(Issue(sev, F, line, msg))

    raw = _blank_comments(text).split("\n")
    # ---- front matter
    body_from = 0
    if raw and raw[0].strip() == "---":
        for i in range(1, len(raw)):
            if raw[i].strip() == "---":
                try:
                    plan.props = yaml.safe_load("\n".join(raw[1:i])) or {}
                except yaml.YAMLError as e:
                    err(1, f"front matter is not valid YAML: {e}")
                    plan.props = {}
                if not isinstance(plan.props, dict):
                    plan.props = {}
                body_from = i + 1
                break
        else:
            err(1, "front matter is not closed with ---")
    fmt = plan.props.get("plan_format")
    if fmt is None:
        err(1, f"front matter must hold plan_format: {PLAN_FORMAT}")
    elif fmt != PLAN_FORMAT:
        err(1, f"plan_format {fmt!r} is not supported here (this parser reads {PLAN_FORMAT})")

    # ---- walk the lines
    cur_sec: Section | None = None
    cur_grp: Group | None = None
    fence = None
    fence_lang = ""
    fence_start = 0
    fence_lines: list = []
    para: list = []              # (line, text) of the current paragraph
    state = "pre"                # pre | table | post   (position inside a group)
    tbl: list = []
    after_lines: list = []

    def flush_para():
        nonlocal para
        if para and cur_grp is not None and state == "pre":
            cur_grp._paras.append(para)
        para = []

    def close_table():
        nonlocal tbl, state
        if cur_grp is not None and tbl:
            _read_table(plan, cur_grp, tbl, err)
        tbl = []

    def finish_group():
        nonlocal cur_grp, state
        flush_para()
        close_table()
        if cur_grp is not None:
            _finish_group(plan, cur_grp, after_lines, err)
        cur_grp = None
        state = "pre"
        after_lines.clear()

    i = body_from
    n = len(raw)
    seen_tier = {}
    while i < n:
        ln = i + 1
        s = raw[i]
        m = re.match(r"^\s*(`{3,}|~{3,})\s*([\w-]*)", s)
        if fence is None and m:
            fence, fence_lang, fence_start, fence_lines = m.group(1)[0], m.group(2).lower(), ln, []
            i += 1
            continue
        if fence is not None:
            if m and m.group(1)[0] == fence and not m.group(2):
                if fence_lang == "gate" and cur_sec is not None and cur_sec.kind == "gate":
                    cur_sec.gates.append(_parse_gate(fence_start, fence_lines, err))
                elif fence_lang == "gate":
                    err(fence_start, "a gate block belongs under the `## Decision gate` heading")
                fence = None
            else:
                fence_lines.append((ln, s))
            i += 1
            continue
        h = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", s)
        if h:
            level, title = len(h.group(1)), h.group(2).strip()
            if level <= 2:
                finish_group()
                cur_sec = None
                if level == 2:
                    cur_sec = _section_from_heading(plan, ln, title, seen_tier, err)
                    if cur_sec is not None:
                        plan.sections.append(cur_sec)
                i += 1
                continue
            if level == 3 and cur_sec is not None and cur_sec.kind in ("tier", "reserved"):
                finish_group()
                cur_grp = _group_from_heading(plan, cur_sec, ln, title, err)
                cur_grp._paras = []
                cur_sec.groups.append(cur_grp)
                state = "pre"
                i += 1
                continue
            if level >= 3 and cur_grp is not None:      # deeper headings inside a group end it
                finish_group()
            i += 1
            continue
        if cur_grp is not None:
            is_row = s.lstrip().startswith("|")
            if state == "pre":
                if is_row:
                    flush_para()
                    state = "table"
                    tbl = [(ln, s)]
                elif s.strip() == "":
                    flush_para()
                else:
                    para.append((ln, s))
            elif state == "table":
                if is_row:
                    tbl.append((ln, s))
                else:
                    close_table()
                    state = "post"
                    if s.strip():
                        after_lines.append((ln, s))
            else:   # post
                if is_row:
                    err(ln, f"group '{cur_grp.title}' has a second table; a group holds exactly one table")
                elif s.strip():
                    after_lines.append((ln, s))
        elif cur_sec is not None and cur_sec.kind == "tier" and s.lstrip().startswith("|"):
            err(ln, f"table outside a group in '{cur_sec.title}'; put it under a `### Group (designators)` heading")
            while i + 1 < n and raw[i + 1].lstrip().startswith("|"):
                i += 1
        i += 1
    finish_group()
    if fence is not None:
        err(fence_start, "code fence is not closed")

    _lint_plan(plan, bom, actions, err)
    return plan


# --------------------------------------------------------------------------- sections and groups
def _section_from_heading(plan, ln, title, seen, err):
    m = re.match(r"^Tier\s*(\d+)\s*[—–-]+\s*(Unpowered|Powered)\s*(\(deeper review\))?\s*$", title, flags=re.I)
    if m:
        tier, mode = int(m.group(1)), m.group(2).lower()
        deeper = bool(m.group(3))
        if tier not in (1, 2):
            err(ln, f"Tier {tier}: only Tier 1 and Tier 2 exist in plan_format 1")
        if tier == 2 and not deeper:
            err(ln, "Tier 2 heading must end with (deeper review)", "warning")
        if tier == 1 and deeper:
            err(ln, "(deeper review) belongs to Tier 2 headings only", "warning")
        key = (tier, mode)
        if key in seen:
            err(ln, f"duplicate section '{title}' (first at line {seen[key]})")
        seen[key] = ln
        return Section(ln, title, "tier", tier, mode, deeper)
    if re.match(r"^Tier\b", title, flags=re.I):
        err(ln, f"malformed tier heading '{title}'. Write `## Tier 1 — Unpowered`, `## Tier 1 — Powered`, "
                "`## Tier 2 — Unpowered (deeper review)` or `## Tier 2 — Powered (deeper review)`")
        return None
    if title.lower() == "decision gate":
        if any(s.kind == "gate" for s in plan.sections):
            err(ln, "duplicate `## Decision gate` section")
        return Section(ln, title, "gate")
    if title.lower() in RESERVED_SECTIONS:
        return Section(ln, title, "reserved", mode=title.lower())
    return None


def _split_designators(s):
    out = []
    for t in re.split(r"[,;]", s):
        t = t.strip()
        if t and t != EMPTY:
            out.append(t)
    return out


def _group_from_heading(plan, sec, ln, title, err):
    m = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", title)
    if not m or not m.group(1).strip():
        err(ln, f"group heading must be `### <Group title> (<designators covered, comma separated>)`; got '{title}'")
        return Group(ln, title, [], sec)
    ds = _split_designators(m.group(2))
    for d in ds:
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_+\-]*", d):
            err(ln, f"group designator '{d}' is not a plain designator")
    return Group(ln, m.group(1).strip(), ds, sec)


def _finish_group(plan, g, after_lines, err):
    paras = getattr(g, "_paras", [])
    g._paras = []
    texts = []
    for p in paras:
        lines = []
        for ln, s in p:
            m = re.match(r"^\s*Actions\s*:\s*(.*)$", s)
            if m:
                for a in re.split(r"[,\s]+", m.group(1).strip()):
                    if a:
                        if re.fullmatch(r"[A-Za-z_][\w-]*", a):
                            g.actions.append((a, ln))
                        else:
                            err(ln, f"action id '{a}' is not a plain id")
            else:
                lines.append(s.strip())
        if lines:
            texts.append(" ".join(lines))
    if len(texts) > 1:
        err(g.line, f"group '{g.title}' has {len(texts)} instruction paragraphs; a group has at most one (put detail in the "
                    "per-test list after the table)")
    g.paragraph = "\n\n".join(texts)
    if not g.tests and g.table_line == 0:
        err(g.line, f"group '{g.title}' has no table", "warning" if g.section and g.section.kind == "reserved" else "error")
    # per-test instruction list directly after the table: `- U03: text`
    by_id = {t.id: t for t in g.tests}
    cur = None
    for ln, s in after_lines:
        m = re.match(r"^\s*[-*]\s+`?([A-Za-z][A-Za-z0-9_]*)`?\s*:\s*(.*)$", s)
        if m:
            tid = m.group(1)
            if tid not in by_id:
                err(ln, f"per-test instruction for '{tid}', which is not in this group's table")
                cur = None
            else:
                cur = by_id[tid]
                cur.instructions = m.group(2).strip()
        elif cur is not None and s.startswith((" ", "\t")) and s.strip():
            cur.instructions += " " + s.strip()
        elif s.strip():
            err(ln, "text after the table that is not a per-test instruction (`- ID: text`) is ignored", "warning")
            cur = None


# --------------------------------------------------------------------------- tables
def _read_table(plan, g, tbl, err):
    g.table_line = tbl[0][0]
    hdr_ln, hdr = tbl[0]
    if len(tbl) < 2 or not re.match(r"^\s*\|?\s*:?-{2,}", tbl[1][1]):
        err(hdr_ln, "table header must be followed by a separator line")
        return
    heads = [_norm_header(c) for c in _cells(hdr)]
    ok = True
    for h in heads:
        if h not in COLUMNS:
            err(hdr_ln, f"unknown column '{h}'. The columns, in order: {' | '.join(COLUMNS)}")
            ok = False
    if ok:
        if len(set(heads)) != len(heads):
            err(hdr_ln, "duplicate column in the header")
            ok = False
        for c in COLUMNS:
            if c not in heads:
                err(hdr_ln, f"missing column '{c}'. The columns, in order: {' | '.join(COLUMNS)}")
                ok = False
        if ok and heads != COLUMNS:
            err(hdr_ln, f"columns out of order. Expected: {' | '.join(COLUMNS)}")
            ok = False
    if not ok:
        return
    for ln, row in tbl[2:]:
        cells = _cells(row)
        if len(cells) != len(COLUMNS):
            err(ln, f"row has {len(cells)} cells, the header has {len(COLUMNS)} (an unescaped | inside a cell? write \\|)")
            continue
        d = dict(zip(COLUMNS, cells))
        odd = [k for k, v in d.items() if v.strip() in ("", "-", "–")]
        if odd:
            err(ln, f"{d['ID']}: empty cell(s) {', '.join(odd)} should be written as —", "warning")
        g.tests.append(Test(ln, d["ID"].strip("` "), d["Test"], d, g))


# --------------------------------------------------------------------------- cell parsing / lint
def _num(s):
    return float(s.replace("−", "-"))


def parse_stop(cell: str):
    """('below', 5.0, text) | ('above', ..) | ('outside', (a, b), text) | ('equals', 'no', text) | None for empty; raises ValueError."""
    c = cell.strip()
    if _empty(c):
        return None
    m = re.match(rf"^(below|above)\s+({NUM})\s*:\s*(\S.*)$", c, flags=re.I | re.S)
    if m:
        return (m.group(1).lower(), _num(m.group(2)), m.group(3).strip())
    m = re.match(rf"^outside\s+({NUM})\s*\.\.\s*({NUM})\s*:\s*(\S.*)$", c, flags=re.I | re.S)
    if m:
        a, b = _num(m.group(1)), _num(m.group(2))
        if a > b:
            raise ValueError("outside A..B needs A not above B")
        return ("outside", (a, b), m.group(3).strip())
    m = re.match(r"^on\s+(No|Yes)\s*:\s*(\S.*)$", c, flags=re.I | re.S)
    if m:
        return ("equals", m.group(1).lower(), m.group(2).strip())
    raise ValueError("Stop if must be `below N: text`, `above N: text`, `outside A..B: text`, `on No: text` or —")


def parse_outlier(cell: str):
    """{'ratio': [lo, hi]} | {'pct': p, 'floor': d, 'floor_unit': u} | None; raises ValueError."""
    c = cell.strip().replace("−", "-")
    if _empty(c):
        return None
    m = re.match(r"^x\s*(\d+(?:\.\d+)?)\s*\.\.\s*x\s*(\d+(?:\.\d+)?)$", c, flags=re.I)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if not (0 < lo < 1 < hi):
            raise ValueError("ratio band xLO..xHI needs 0 < LO < 1 < HI, for example x0.5..x2")
        return {"ratio": [lo, hi]}
    m = re.match(r"^(?:±|\+-|\+/-)\s*(\d+(?:\.\d+)?)\s*%\s*(?:\(\s*min\s+(\d+(?:\.\d+)?)\s*([^)]*?)\s*\))?$", c)
    if m:
        out = {"pct": float(m.group(1))}
        if m.group(2) is not None:
            out["floor"] = float(m.group(2))
            if m.group(3):
                out["floor_unit"] = m.group(3)
        if out["pct"] <= 0:
            raise ValueError("percent band must be above 0")
        return out
    raise ValueError("Outlier must be `xLO..xHI` (for example x0.5..x2), `±P%` or `±P% (min D unit)`, or —")


def parse_source(cell: str, cls: str):
    """-> (source dict | None, closes_on | None, problem | None)."""
    c = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", r"\1", cell.strip())
    if _empty(c):
        return None, None, None
    m = re.match(r"^closes on\s*:\s*(\S.*)$", c, flags=re.I | re.S)
    if m:
        return {"type": cls if cls in ("guess", "tbr") else "guess"}, m.group(1).strip(), None
    m = re.match(r"^calc\s+(C\d+(?:\s*,\s*C\d+)*)$", c, flags=re.I)
    if m:
        refs = re.sub(r"\s+", "", m.group(1).upper())
        return {"type": "design_calc", "doc": "calc.md", "ref": refs}, None, None
    m = re.match(r"^(?P<doc>.+?)\s+p\.(?P<page>\d+)(?:\s+(?P<what>Table|Section|Fig(?:ure)?\.?|§)\s*(?P<ref>.*))?$", c)
    if m:
        src = {"type": "datasheet", "doc": m.group("doc").strip(), "page": int(m.group("page"))}
        if m.group("what"):
            src["section" if m.group("what") in ("Section", "§") else "table"] = (m.group("ref") or "").strip() or m.group("what")
        return src, None, None
    w = c.split()[0].lower().rstrip(":")
    if w in GUESS_SOURCE_WORDS:
        return {"type": w}, None, None
    return None, None, "source must be `DOC p.N Table X`, `calc Cn` or, for guess and tbr limits, `closes on: ...`"


def _limit_for(t: Test, c: dict, err):
    """Build (limit, test fields) for a row; errors against t.line. Returns dict or None."""
    L = t.line
    kind = c["Kind"].strip().lower()
    cls = c["Class"].strip().lower()
    out = {}
    if kind not in KINDS:
        err(L, f"{t.id}: Kind '{c['Kind']}' must be one of {', '.join(KINDS)}")
        return None
    if cls not in CLASSES:
        err(L, f"{t.id}: Class '{c['Class']}' must be one of {', '.join(CLASSES)}")
        return None
    src, closes, prob = parse_source(c["Source"], cls)
    if prob:
        err(L, f"{t.id}: {prob}")
    if cls in ("hard", "guess") and _empty(c["Source"]):
        err(L, f"{t.id}: {cls} limit without a source")
    if cls == "hard" and not _empty(c["Source"]):
        if closes is not None or (src and src.get("type") in GUESS_SOURCE_WORDS):
            err(L, f"{t.id}: hard limit with a guess source; make the class guess or tbr, or cite a datasheet page or a calc")
    if cls in ("guess", "tbr") and not closes:
        if not _empty(c["Source"]) and not prob:
            err(L, f"{t.id}: {cls} limit needs `closes on: ...` in Source (what measurement closes it)")
        elif _empty(c["Source"]) and cls == "tbr":
            err(L, f"{t.id}: tbr limit needs `closes on: ...` in Source")
    if cls == "record" and not _empty(c["Source"]):
        err(L, f"{t.id}: a record test needs no source", "warning")
    lim = {"class": cls}
    mn, mx = c["Min"].strip().replace("−", "-"), c["Max"].strip().replace("−", "-")
    if kind == "number":
        for k, v in (("min", mn), ("max", mx)):
            if not _empty(v):
                if re.fullmatch(NUM, v):
                    lim[k] = float(v) if not re.fullmatch(r"[-+]?\d+", v) else int(v)
                else:
                    err(L, f"{t.id}: {k.title()} '{v}' is not a number")
        if "min" in lim and "max" in lim and lim["min"] > lim["max"]:
            err(L, f"{t.id}: Min is above Max")
        if cls in ("hard", "guess") and "min" not in lim and "max" not in lim:
            err(L, f"{t.id}: a {cls} number test needs Min and/or Max")
        if cls == "record" and ("min" in lim or "max" in lim):
            err(L, f"{t.id}: a record test has no limit; Min and Max are ignored", "warning")
            lim.pop("min", None)
            lim.pop("max", None)
        u = c["Unit"].strip()
        if _empty(u):
            err(L, f"{t.id}: a number test needs a Unit")
        else:
            u = UNIT_ALIAS.get(u, u)
            if u not in UNITS:
                err(L, f"{t.id}: Unit '{u}' is not in the vocabulary ({', '.join(UNITS)})")
            out["unit"] = u
    elif kind == "yn":
        if not (_empty(mn) and _empty(mx)):
            err(L, f"{t.id}: a yn test takes Min and Max as —; it passes on Yes")
        if cls in ("hard", "guess"):
            lim["equals"] = "yes"
    elif kind == "choice":
        if not t.options:
            err(L, f"{t.id}: a choice test needs `(options: a / b / c)` at the end of the Test cell")
        if not (_empty(mn) and _empty(mx)):
            err(L, f"{t.id}: a choice test takes Min and Max as —")
        if cls in ("hard", "guess"):
            err(L, f"{t.id}: a choice test records the option chosen; class must be record or tbr")
    else:   # text
        if not (_empty(mn) and _empty(mx)):
            err(L, f"{t.id}: a text test takes Min and Max as —")
        if cls in ("hard", "guess"):
            err(L, f"{t.id}: a text test cannot have a {cls} limit; class record")
    if kind != "number" and not _empty(c["Unit"]):
        err(L, f"{t.id}: Unit applies to number tests only", "warning")
    if src and cls in ("hard",):
        lim["source"] = src
    elif cls in ("guess", "tbr"):
        lim["source"] = {"type": cls}
        if closes:
            lim["closes_on"] = closes
    elif src and cls == "tbr":
        lim["source"] = src
    out["limit"] = lim
    out["kind"] = kind
    return out


def _lint_plan(plan, bom, actions, err):
    ids = {}
    ids_ci = {}
    width = {}
    bom_des = None
    if bom is not None:
        bom_des = {d for b in bom for d in (b.get("designators") or [])}
        plan.bom_checked = True
    else:
        err(1, "no BOM given: designators are not checked against the BOM (use --protocol)", "warning")
    section_ids = {}
    for sec in plan.sections:
        if sec.kind == "reserved":
            if any(g.tests for g in sec.groups):
                err(sec.line, f"section '{sec.title}' is reserved in plan_format 1: it is parsed and linted, not generated", "warning")
        for g in sec.groups:
            if bom_des is not None:
                for d in g.designators:
                    if d not in bom_des:
                        err(g.line, f"group designator '{d}' is not in the BOM")
            if sec.kind == "tier":
                sid = section_id(sec, g)
                step = sec.mode
                if (step, sid) in section_ids:
                    err(g.line, f"group '{g.title}' makes the same section id '{sid}' as the group at line {section_ids[(step, sid)]}")
                section_ids[(step, sid)] = g.line
                for a, aln in g.actions:
                    if actions is not None and a not in actions:
                        err(aln, f"action '{a}' is not defined in the protocol")
            for t in g.tests:
                c = t.cells
                tid = t.id
                if not re.fullmatch(r"[A-Z][A-Z0-9_]*[a-z]?", tid):
                    err(t.line, f"ID '{tid}': letters (upper case), digits and underscore, starting with a letter; a lower-case suffix letter is allowed")
                if tid.upper() in RESERVED_IDS or re.fullmatch(r"B\d+", tid):
                    err(t.line, f"ID '{tid}' is reserved (a voice or command word, or a board name)")
                if tid in ids:
                    err(t.line, f"duplicate ID '{tid}' (first at line {ids[tid]})")
                elif tid.upper() in ids_ci:
                    err(t.line, f"ID '{tid}' differs only in case from '{ids_ci[tid.upper()][0]}' (line {ids_ci[tid.upper()][1]})")
                ids.setdefault(tid, t.line)
                ids_ci.setdefault(tid.upper(), (tid, t.line))
                m = re.fullmatch(r"([A-Z]+)(\d+)", tid)
                if m:
                    w = width.setdefault(m.group(1), (len(m.group(2)), t.line, tid))
                    if len(m.group(2)) != w[0] and not (len(m.group(2)) > 2 and w[0] > 2):
                        err(t.line, f"ID '{tid}' has a different digit width from '{w[2]}' (line {w[1]}); use one width per prefix")
                # options in the Test cell
                title = t.title
                mo = re.search(r"\(options:\s*(.+)\)\s*$", title, flags=re.I | re.S)
                if mo:
                    t.options = [o.strip() for o in mo.group(1).split(" / ") if o.strip()]
                    t.title = title[:mo.start()].strip()
                    if len(t.options) < 2:
                        err(t.line, f"{tid}: (options: ...) needs at least two options separated by ' / '")
                if _empty(t.title):
                    err(t.line, f"{tid}: the Test cell is empty")
                res = _limit_for(t, c, err)
                t.parsed = res or {}
                # stop, outlier, inputs
                try:
                    t.stop = parse_stop(c["Stop if"])
                except ValueError as e:
                    t.stop = None
                    err(t.line, f"{tid}: malformed Stop if '{c['Stop if'].strip()[:60]}': {e}")
                if t.stop and res and res["kind"] in ("number",) and t.stop[0] == "equals":
                    err(t.line, f"{tid}: `on No` is for yn tests")
                if t.stop and res and res["kind"] == "yn" and t.stop[0] != "equals":
                    err(t.line, f"{tid}: a yn test takes `on No: text` as its stop rule")
                if t.stop and res and res["kind"] in ("text", "choice"):
                    err(t.line, f"{tid}: a {res['kind']} test takes no stop rule")
                try:
                    t.outlier = parse_outlier(c["Outlier"])
                except ValueError as e:
                    t.outlier = None
                    err(t.line, f"{tid}: malformed Outlier '{c['Outlier'].strip()[:40]}': {e}")
                if t.outlier and res and res["kind"] != "number":
                    err(t.line, f"{tid}: Outlier applies to number tests only")
                    t.outlier = None
                inp = c["Inputs"].strip()
                if _empty(inp):
                    t.inputs = list(DEFAULT_INPUTS)
                else:
                    t.inputs = [x.strip().lower() for x in inp.split(",") if x.strip()]
                    for x in t.inputs:
                        if x not in INPUTS:
                            err(t.line, f"{tid}: input '{x}' must be one of {', '.join(INPUTS)}")
                    if "action" in t.inputs and not g.actions and sec.kind == "tier":
                        err(t.line, f"{tid}: Inputs has action but the group declares no `Actions:` line", "warning")
                # names
                t.probe = []
                for key, pole in (("Probe +", "+"), ("Probe -", "-")):
                    v = c[key].strip()
                    if not _empty(v):
                        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_+\-]*(\.[A-Za-z0-9_]+)?", v):
                            err(t.line, f"{tid}: {key} '{v}' is not a designator or pad name")
                        t.probe.append({"point": v, "pole": pole})
                        if bom_des is not None and v.split(".")[0] not in bom_des:
                            err(t.line, f"{tid}: {key} '{v}' is not in the BOM", "warning")
                t.nets = _items(c["Nets"])
                for nn in t.nets:
                    if not re.fullmatch(r"[A-Za-z0-9_+\-/$.]+", nn):
                        err(t.line, f"{tid}: net '{nn}' has characters a net name does not have")
                t.hl_spec = _items(c["Highlight"])
                t.hl_parts, t.hl_pins = [], []
                for h in t.hl_spec:
                    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_+\-]*(\.[A-Za-z0-9_]+)?", h):
                        err(t.line, f"{tid}: Highlight '{h}' must be a designator (R3, TP1) or designator.pin (IC1.12)")
                        continue
                    des = h.split(".")[0]
                    if bom_des is not None and des not in bom_des:
                        err(t.line, f"{tid}: Highlight '{h}': {des} is not in the BOM")
                    if des not in t.hl_parts:
                        t.hl_parts.append(des)
                    if "." in h:
                        t.hl_pins.append(h)
                if sec.kind == "tier" and not (t.hl_spec or t.nets) and res and res["kind"] != "text":
                    pass
    # gates
    ids_set = set(ids)
    for s in plan.sections:
        if s.kind != "gate":
            continue
        if not s.gates:
            err(s.line, "the `## Decision gate` section holds no ```gate block", "warning")
        for gt in s.gates:
            plan.gates.append(gt)
            for rule in gt["rules"]:
                if rule["type"] == "outlier":
                    withband = [t for _s, _g, t in plan.tests() if getattr(t, "outlier", None)]
                    if not withband:
                        err(rule["line"], "outlier rule, but no test has an Outlier entry")
            for key in ("after", "to"):
                v = gt.get(key)
                if v and not any(sc.kind == "tier" and sc.tier == v["tier"] and (not v["mode"] or sc.mode == v["mode"]) for sc in plan.sections):
                    err(gt.get(key + "_line", gt["line"]), f"gate {key}: no section for Tier {v['tier']}{(' ' + v['mode']) if v['mode'] else ''}")
    if not any(s.kind == "tier" for s in plan.sections):
        err(1, "the plan has no `## Tier N — ...` section")


def section_id(sec: Section, g: Group) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", g.title.lower()).strip("_")[:40].strip("_")
    return f"t{sec.tier}_{slug}"


# --------------------------------------------------------------------------- decision gate block
def _tier_ref(v, line, err):
    m = re.match(r"^Tier\s*(\d+)\s*(Unpowered|Powered)?\s*$", v.strip(), flags=re.I)
    if not m:
        err(line, f"expected `Tier N` or `Tier N Unpowered|Powered`, got '{v.strip()}'")
        return None
    return {"tier": int(m.group(1)), "mode": (m.group(2) or "").lower()}


def _parse_gate(start, lines, err):
    gt = {"line": start, "after": None, "to": {"tier": 2, "mode": ""}, "rules": []}
    for ln, raw in lines:
        s = re.sub(r"\s+#.*$", "", raw).strip().lstrip("-* ").strip()
        if not s:
            continue
        m = re.match(r"^(after|to)\s*:\s*(.*)$", s, flags=re.I)
        if m:
            gt[m.group(1).lower()] = _tier_ref(m.group(2), ln, err)
            gt[m.group(1).lower() + "_line"] = ln
            continue
        if re.fullmatch(r"any (out of band|fail)", s, flags=re.I):
            gt["rules"].append({"type": "out_of_band", "line": ln})
        elif re.fullmatch(r"any stop", s, flags=re.I):
            gt["rules"].append({"type": "stop", "line": ln})
        elif re.fullmatch(r"hold power", s, flags=re.I):
            gt["rules"].append({"type": "hold_power", "line": ln})
        else:
            m = re.fullmatch(r"outlier from (\d+) boards", s, flags=re.I)
            if m:
                gt["rules"].append({"type": "outlier", "min_boards": int(m.group(1)), "line": ln})
                continue
            m = re.fullmatch(rf"halt when more than ({NUM}) ?% of boards are out of band on one test", s, flags=re.I)
            if m:
                gt["rules"].append({"type": "halt", "pct": float(m.group(1)), "line": ln})
                continue
            err(ln, f"unknown gate rule '{s}'. Rules: `any out of band`, `any stop`, `outlier from N boards`, `hold power`, "
                    "`halt when more than P % of boards are out of band on one test`")
    if gt["after"] is None:
        err(start, "gate block needs `after: Tier N [Unpowered|Powered]`")
    if not gt["rules"]:
        err(start, "gate block holds no rules")
    return gt


# --------------------------------------------------------------------------- generation
def generate(plan: Plan, library: dict | None = None) -> dict:
    """The protocol steps the plan defines: {'steps': [...], 'tier_gates': [...]}. `library` = {action id: action dict} from the
    protocol; a declared action takes its title, plugin, params and proposes from there."""
    library = library or {}
    steps: dict = {}
    for sec in plan.sections:
        if sec.kind != "tier":
            continue
        st = steps.setdefault(sec.mode, {"id": sec.mode, "kind": sec.mode, "advance": "row" if sec.mode == "unpowered" else "board",
                                         "sections": [], "plan_generated": True})
        for g in sec.groups:
            s = {"id": section_id(sec, g), "title": g.title, "designators": list(g.designators), "tier": sec.tier,
                 "instructions": g.paragraph, "tests": [], "actions": [], "plan_line": g.line}
            if sec.tier >= 2:
                s["applies_to"] = "operator"
                s["conditional"] = True
            for a, _ln in g.actions:
                base = library.get(a)
                s["actions"].append(dict(base) if base else {"id": a, "title": a, "plugin": "", "params": {}, "proposes": []})
            for t in g.tests:
                s["tests"].append(_test_dict(t, sec))
            st["sections"].append(s)
    gates = []
    for gt in plan.gates:
        gates.append({"after": gt["after"], "to": gt["to"], "rules": [{k: v for k, v in r.items() if k != "line"} for r in gt["rules"]]})
    return {"steps": list(steps.values()), "tier_gates": gates}


def _test_dict(t: Test, sec: Section) -> dict:
    p = t.parsed
    d = {"id": t.id, "title": t.title, "kind": p.get("kind", "text")}
    if p.get("unit"):
        d["unit"] = p["unit"]
    if t.options:
        d["options"] = list(t.options)
    d["limit"] = p.get("limit", {"class": "record"})
    if t.probe:
        d["probe"] = [dict(x) for x in t.probe]
    if t.nets:
        d["nets"] = list(t.nets)
    if t.hl_parts:
        d["highlight"] = list(t.hl_parts)
    if t.hl_pins:
        d["highlight_pins"] = list(t.hl_pins)
    if t.stop:
        k, v, text = t.stop
        d["stop_if"] = {("equals" if k == "equals" else k): (list(v) if k == "outside" else v), "text": text}
    if t.outlier:
        d["outlier"] = dict(t.outlier)
    d["inputs"] = list(t.inputs)
    if t.instructions:
        d["instructions"] = t.instructions
    d["tier"] = sec.tier
    d["plan_line"] = t.line
    return d


def to_yaml(plan: Plan, library: dict | None = None) -> str:
    frag = generate(plan, library)
    head = (f"# GENERATED from {Path(plan.path).name} @ {plan.hash}, do not edit.\n"
            f"# Edit the plan and run: polybench plan import {Path(plan.path).name} --out <this file>\n")
    return head + yaml.safe_dump(frag, sort_keys=False, allow_unicode=True, width=140)


# --------------------------------------------------------------------------- coverage
def coverage(tier_steps: list, bom: list | None, plan: Plan | None = None) -> dict:
    """Counts for step 4: tests per step, tier and section; designators covered against the BOM (from the generated steps)."""
    per = []
    ntests = 0
    cov = {}
    for st in tier_steps:
        for sec in st.get("sections", []):
            n = len(sec.get("tests", []))
            ntests += n
            per.append({"step": st["id"], "section": sec["id"], "title": sec["title"], "tier": sec.get("tier"), "tests": n,
                        "designators": sec.get("designators", [])})
            for d in sec.get("designators", []):
                cov.setdefault(d, set()).add(sec["id"])
            for t in sec.get("tests", []):
                for d in (t.get("highlight") or []) + [p["point"] for p in t.get("probe", [])]:
                    cov.setdefault(d, set()).add(t["id"])
    tiers = {}
    for r in per:
        k = f"Tier {r['tier']} {r['step']}"
        tiers[k] = tiers.get(k, 0) + r["tests"]
    out = {"tests": ntests, "sections": per, "by_tier": tiers}
    if bom is not None:
        allb = [(d, b) for b in bom for d in b.get("designators", [])]
        have = [d for d, _ in allb if d in cov]
        miss = [{"designator": d, "line": b["line"], "value": b.get("value") or b.get("mpn") or "", "dnf": bool(b.get("dnf"))}
                for d, b in allb if d not in cov]
        out["bom"] = {"total": len(allb), "covered": len(have), "uncovered": miss}
    return out
