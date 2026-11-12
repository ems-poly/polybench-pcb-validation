# Ported from the earlier voice prototype prg/sheet.py. Changes for Polybench: none.
"""Markdown sheet model: find tables, locate cells by byte offset, replace cells in place.

The file is handled as bytes so that everything outside an edited cell is
preserved exactly (line endings, trailing spaces, odd padding).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

BOARD_RE = re.compile(r"^\s*(B\d+)\b", re.I)
SEP_RE = re.compile(rb"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


@dataclass
class Cell:
    start: int          # byte offset of the first byte after the opening pipe
    end: int            # byte offset of the closing pipe
    text: str           # stripped content


@dataclass
class Row:
    line_no: int
    cells: list[Cell]


@dataclass
class Table:
    index: int
    section: str                 # nearest heading text above the table
    header_line: int
    headers: list[str]
    rows: list[Row] = field(default_factory=list)


@dataclass
class Sheet:
    path: str
    data: bytes
    sha256: str
    tables: list[Table]
    warnings: list[str]


def _split_cells(line: bytes, base: int) -> list[Cell] | None:
    """Split a table line into cells with absolute byte offsets. Escaped pipes stay inside a cell."""
    stripped = line.rstrip(b"\r\n")
    pipes = [i for i, ch in enumerate(stripped) if ch == 0x7C and not (i > 0 and stripped[i - 1] == 0x5C)]
    if len(pipes) < 2:
        return None
    # A table line must start with a pipe (after optional whitespace).
    if stripped[: pipes[0]].strip():
        return None
    last = stripped.rstrip()
    if pipes[-1] != len(last) - 1:
        # no closing pipe: treat the end of text as the end of the last cell
        pipes.append(len(stripped))
    cells = []
    for a, b in zip(pipes, pipes[1:]):
        txt = stripped[a + 1:b].decode("utf-8", "replace").strip()
        cells.append(Cell(base + a + 1, base + b, txt))
    return cells


def parse(data: bytes, path: str = "") -> Sheet:
    lines = data.splitlines(keepends=True)
    offsets, pos = [], 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln)
    tables: list[Table] = []
    warnings: list[str] = []
    section = ""
    in_fence = False
    in_front = bool(lines) and lines[0].strip() == b"---"
    i = 0
    n = len(lines)
    if in_front:
        i = 1
        while i < n and lines[i].strip() != b"---":
            i += 1
        i += 1
    while i < n:
        raw = lines[i]
        s = raw.strip()
        if s.startswith(b"```") or s.startswith(b"~~~"):
            in_fence = not in_fence
            i += 1
            continue
        if in_fence:
            i += 1
            continue
        m = re.match(rb"^(#{1,6})\s+(.*)$", s)
        if m:
            section = m.group(2).decode("utf-8", "replace").strip()
            i += 1
            continue
        if s.startswith(b"|") and i + 1 < n and SEP_RE.match(lines[i + 1].strip()):
            hdr = _split_cells(raw, offsets[i])
            if hdr:
                t = Table(len(tables), section, i, [c.text for c in hdr])
                j = i + 2
                while j < n and lines[j].strip().startswith(b"|"):
                    cells = _split_cells(lines[j], offsets[j])
                    if cells is None:
                        break
                    if len(cells) != len(t.headers):
                        warnings.append(
                            f"line {j + 1}: row has {len(cells)} cells, header has {len(t.headers)}; row skipped")
                    else:
                        t.rows.append(Row(j, cells))
                    j += 1
                tables.append(t)
                i = j
                continue
        i += 1
    return Sheet(path, data, hashlib.sha256(data).hexdigest(), tables, warnings)


def load(path: str) -> Sheet:
    with open(path, "rb") as f:
        return parse(f.read(), path)


def render_cell(value: str, original: bytes) -> bytes:
    """Bytes to put between the pipes: one space of padding each side, pipes escaped."""
    v = value.replace("|", "\\|").replace("\n", " ").strip()
    return b" " + v.encode("utf-8") + b" "


def apply_edits(data: bytes, edits: list[tuple[int, int, str]]) -> bytes:
    """edits = [(start, end, new_text)]; replaces the bytes between pipes. Offsets refer to `data`."""
    out = bytearray()
    cur = 0
    for start, end, text in sorted(edits):
        if start < cur:
            raise ValueError("overlapping edits")
        out += data[cur:start]
        out += render_cell(text, data[start:end])
        cur = end
    out += data[cur:]
    return bytes(out)
