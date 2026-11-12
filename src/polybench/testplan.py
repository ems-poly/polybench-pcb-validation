"""Test plan viewer: a small, safe markdown renderer (no third-party package, no CDN) and the file facts shown above it.

Safety by construction: every character of the source is HTML-escaped first; the only tags in the output are the ones this module writes.
Raw HTML in the markdown therefore shows as text and never runs. Link schemes are limited to http, https and mailto.
Obsidian specifics: `%% ... %%` comments are removed, front matter is dropped, `[[name]]` / `[[name|label]]` render as a plain label,
or as an in-page link when `name.md` sits next to the plan, `> [!type] Title` blocks render as callouts.
"""
from __future__ import annotations

import html
import re
import subprocess
from datetime import datetime
from pathlib import Path

MAX_BYTES = 2_000_000


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


# ---------------------------------------------------------------- source clean-up
def strip_comments(text: str) -> str:
    """Remove Obsidian %% ... %% comments (also over several lines), but not inside fenced code."""
    out, buf, fence = [], [], None
    for ln in text.split("\n"):
        m = re.match(r"^\s*(`{3,}|~{3,})", ln)
        if fence is None and m:
            out.append(_strip_inline("\n".join(buf))); buf = []
            fence = m.group(1)[0]; out.append(ln)
        elif fence is not None:
            out.append(ln)
            if m and m.group(1)[0] == fence:
                fence = None
        else:
            buf.append(ln)
    out.append(_strip_inline("\n".join(buf)))
    return "\n".join(out)


def _strip_inline(t: str) -> str:
    t = re.sub(r"%%.*?%%", "", t, flags=re.S)
    return re.sub(r"%%.*\Z", "", t, flags=re.S)       # an unclosed comment hides the rest of that block, as Obsidian does


def strip_front_matter(text: str) -> tuple[str, dict]:
    m = re.match(r"\A---[ \t]*\n(.*?\n)---[ \t]*(\n|\Z)", text, flags=re.S)
    if not m:
        return text, {}
    props = {}
    for ln in m.group(1).split("\n"):
        k = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", ln)
        if k:
            props[k.group(1)] = k.group(2).strip()
    return text[m.end():], props


# ---------------------------------------------------------------- inline
class _Ctx:
    def __init__(self, siblings):
        self.siblings = siblings            # callable(name) -> file name next to the plan, or None
        self.stash: list[str] = []

    def hold(self, h: str) -> str:
        self.stash.append(h)
        return f"\x00{len(self.stash) - 1}\x00"

    def restore(self, s: str) -> str:
        for _ in range(4):
            s = re.sub(r"\x00(\d+)\x00", lambda m: self.stash[int(m.group(1))], s)
        return s


def _target_link(ctx: _Ctx, target: str, label_html: str) -> str:
    target = target.split("#")[0].strip()
    sib = ctx.siblings(target) if target else None
    if sib:
        return f'<a href="#" class="tp-link" data-tp-open="{_esc(sib)}">{label_html}</a>'
    return f'<span class="wl">{label_html}</span>'


def inline(text: str, ctx: _Ctx) -> str:
    text = _esc(text)

    def code(m):
        return ctx.hold(f"<code>{m.group(2).strip()}</code>")
    text = re.sub(r"(`+)(.+?)\1", code, text)

    def wiki(m):
        tgt, lab = m.group(1), m.group(2)
        label = lab if lab is not None and lab.strip() else tgt.split("#")[0].split("/")[-1] if tgt.split("#")[0] else tgt
        return ctx.hold(_target_link(ctx, html.unescape(tgt), _fmt(label, ctx)))
    text = re.sub(r"\[\[([^\[\]|]+)(?:\|([^\[\]]*))?\]\]", wiki, text)

    def image(m):
        return ctx.hold(f'<span class="faint">[image: {m.group(1) or "figure"}]</span>')
    text = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;[^)]*&quot;)?\)", image, text)

    def link(m):
        lab, url = m.group(1), html.unescape(m.group(2))
        if re.match(r"(?i)^(https?://|mailto:)", url):
            return ctx.hold(f'<a href="{_esc(url)}" target="_blank" rel="noopener noreferrer">{_fmt(lab, ctx)}</a>')
        if url.startswith("#"):
            return ctx.hold(f'<span class="wl">{_fmt(lab, ctx)}</span>')
        return ctx.hold(_target_link(ctx, url.rsplit("/", 1)[-1], _fmt(lab, ctx)))
    text = re.sub(r"\[([^\[\]]+)\]\(([^)\s]+)\)", link, text)
    return ctx.restore(_fmt(text, ctx))


def _fmt(t: str, ctx: _Ctx) -> str:
    t = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])", r"<em>\1</em>", t)
    t = re.sub(r"(?<!\w)_(?=[^\s_])(.+?)(?<=[^\s_])_(?!\w)", r"<em>\1</em>", t)
    t = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<del>\1</del>", t)
    t = re.sub(r"==(?=\S)(.+?)(?<=\S)==", r"<mark>\1</mark>", t)
    return t


# ---------------------------------------------------------------- blocks
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+-]*)")
_LI = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_TSEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    out, cur, i, tick = [], "", 0, False
    while i < len(s):
        c = s[i]
        if c == "`":
            tick = not tick
        if c == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            cur += "|"; i += 2; continue
        if c == "|" and not tick:
            out.append(cur.strip()); cur = ""
        else:
            cur += c
        i += 1
    out.append(cur.strip())
    return out


def blocks(lines: list[str], ctx: _Ctx) -> str:
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        if not ln.strip():
            i += 1; continue
        m = _FENCE.match(ln)
        if m:
            f, lang = m.group(1), m.group(2)
            j, body = i + 1, []
            while j < n and not re.match(r"^\s*" + re.escape(f[0]) + "{" + str(len(f)) + r",}\s*$", lines[j]):
                body.append(lines[j]); j += 1
            out.append(f'<pre class="code"{(" data-lang=" + chr(34) + _esc(lang) + chr(34)) if lang else ""}><code>{_esc(chr(10).join(body))}</code></pre>')
            i = j + 1; continue
        h = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", ln)
        if h:
            lv = len(h.group(1))
            out.append(f"<h{lv}>{inline(h.group(2), ctx)}</h{lv}>"); i += 1; continue
        if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", ln):
            out.append("<hr>"); i += 1; continue
        if ln.lstrip().startswith(">"):
            q = []
            while i < n and lines[i].lstrip().startswith(">"):
                q.append(re.sub(r"^\s*>\s?", "", lines[i])); i += 1
            out.append(_quote(q, ctx)); continue
        if "|" in ln and i + 1 < n and _TSEP.match(lines[i + 1]) and "|" in lines[i + 1] + "|":
            head = _cells(ln)
            al = [("center" if c.strip().startswith(":") and c.strip().endswith(":") else "right" if c.strip().endswith(":") else "left") for c in _cells(lines[i + 1])]
            i += 2
            rows = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(_cells(lines[i])); i += 1
            def cell(tag, c, k):
                a = al[k] if k < len(al) else "left"
                return f'<{tag} style="text-align:{a}">{inline(c, ctx)}</{tag}>'
            out.append('<div class="tp-tw"><table><thead><tr>' + "".join(cell("th", c, k) for k, c in enumerate(head)) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(cell("td", c, k) for k, c in enumerate(r)) + "</tr>" for r in rows) + "</tbody></table></div>")
            continue
        if _LI.match(ln):
            items = []
            while i < n and (_LI.match(lines[i]) or (lines[i].strip() and lines[i].startswith((" ", "\t")) and items)):
                items.append(lines[i]); i += 1
            out.append(_list(items, ctx)); continue
        para = [ln]
        i += 1
        while i < n and lines[i].strip() and not (_FENCE.match(lines[i]) or re.match(r"^#{1,6}\s", lines[i]) or lines[i].lstrip().startswith(">") or _LI.match(lines[i])
                                                  or re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", lines[i]) or ("|" in lines[i] and i + 1 < n and _TSEP.match(lines[i + 1]))):
            para.append(lines[i]); i += 1
        out.append("<p>" + "<br>".join(inline(grp, ctx) for grp in _hard("\n".join(para))) + "</p>")
    return "\n".join(out)


def _hard(s: str) -> list[str]:
    """Split a paragraph at lines that end in two spaces (a hard break); soft breaks stay spaces."""
    parts, cur = [], []
    for ln in s.split("\n"):
        cur.append(ln.strip())
        if ln.endswith("  "):
            parts.append(" ".join(cur)); cur = []
    if cur:
        parts.append(" ".join(cur))
    return parts


def _quote(q: list[str], ctx: _Ctx) -> str:
    m = re.match(r"^\[!([\w-]+)\][+-]?\s*(.*)$", q[0].strip())
    if m:
        kind = re.sub(r"[^a-z0-9-]", "", m.group(1).lower())
        title = m.group(2).strip() or kind.capitalize()
        return f'<div class="callout c-{kind}"><div class="ct">{inline(title, ctx)}</div><div class="cb">{blocks(q[1:], ctx)}</div></div>'
    return f"<blockquote>{blocks(q, ctx)}</blockquote>"


def _list(items: list[str], ctx: _Ctx) -> str:
    def indent(s):
        return len(s.expandtabs(4)) - len(s.expandtabs(4).lstrip())
    base = indent(items[0])
    pos = 0

    def level(ind):
        nonlocal pos
        first = _LI.match(items[pos])
        ordered = bool(re.match(r"\d", first.group(2)))
        h = []
        while pos < len(items):
            ln = items[pos]
            if indent(ln) < ind:
                break
            m = _LI.match(ln)
            if not m or indent(ln) > ind:
                if m and indent(ln) > ind:           # deeper without a parent item: treat as a child of the previous
                    h.append(level(indent(ln)))
                    continue
                h.append(" " + inline(ln.strip(), ctx)); pos += 1; continue
            pos += 1
            body = m.group(3)
            t = re.match(r"^\[([ xX])\]\s+(.*)$", body)
            pre = ""
            if t:
                pre = f'<span class="tk">{"&#9745;" if t.group(1) != " " else "&#9744;"}</span> '
                body = t.group(2)
            h.append(f'<li class="task">{pre}{inline(body, ctx)}' if t else f"<li>{inline(body, ctx)}")
            while pos < len(items) and indent(items[pos]) > ind:
                if _LI.match(items[pos]):
                    h.append(level(indent(items[pos])))
                else:
                    h.append(" " + inline(items[pos].strip(), ctx)); pos += 1
            h.append("</li>")
        tag = "ol" if ordered else "ul"
        return f"<{tag}>{''.join(h)}</{tag}>"
    return level(base)


def render_markdown(text: str, siblings=lambda name: None) -> dict:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = strip_comments(text)
    text, props = strip_front_matter(text)
    ctx = _Ctx(siblings)
    return {"html": blocks(text.split("\n"), ctx), "properties": props}


# ---------------------------------------------------------------- files
def git_info(path: Path) -> dict | None:
    try:
        d = path.parent
        r = subprocess.run(["git", "-C", str(d), "log", "-1", "--format=%h%x09%cs%x09%s", "--", path.name], capture_output=True, text=True, timeout=3)
        if r.returncode != 0 or not r.stdout.strip():
            chk = subprocess.run(["git", "-C", str(d), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True, timeout=3)
            if chk.returncode == 0 and chk.stdout.strip() == "true":
                return {"commit": None, "note": "not committed yet", "dirty": True}
            return None
        h, date, subj = (r.stdout.strip().split("\t") + ["", ""])[:3]
        st = subprocess.run(["git", "-C", str(d), "status", "--porcelain", "--", path.name], capture_output=True, text=True, timeout=3)
        return {"commit": h, "date": date, "subject": subj, "dirty": bool(st.stdout.strip())}
    except Exception:
        return None


def load_plan(path: str | Path, sub: str | None = None) -> dict:
    """Render one plan file (or a .md next to it, `sub`). Never raises: a missing or unreadable file comes back as exists=False."""
    p = Path(path)
    base_dir = p.parent
    if sub:
        cand = (base_dir / sub).resolve()
        if cand.parent != base_dir.resolve() or cand.suffix.lower() != ".md":
            return {"exists": False, "path": str(base_dir / sub), "error": "only markdown files next to the plan can be opened"}
        p = cand
    res = {"path": str(p), "name": p.name, "exists": p.is_file()}
    if not res["exists"]:
        return res
    try:
        raw = p.read_bytes()[:MAX_BYTES].decode("utf-8", "replace")
    except OSError as e:
        return {**res, "exists": False, "error": str(e)}
    st = p.stat()
    res["mtime"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    res["git"] = git_info(p)

    def sib(name: str):
        name = name.strip()
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return None
        for cand in (name, name + ".md"):
            if (base_dir / cand).is_file() and cand.lower().endswith(".md"):
                return cand
        return None
    res.update(render_markdown(raw, sib))
    return res
