"""Load a BOM file chosen on the Project page (card 4) into Polybench BOM lines.

Formats are the ones Polybench already reads:
  .yaml / .yml   a protocol fragment with a top-level `bom:` list (docs/DV_CONTRACT.md), checked by the protocol loader's rules
  .csv / .xlsx   a purchasing BOM, through the embedded Assembly Scanner importer (assembly_scanner/bom.py: Mouser P/N column,
                 designators, MPN, package, fitted/DNF)
The file is referenced, never copied.
"""
from __future__ import annotations

from pathlib import Path

FORMATS = (".yaml", ".yml", ".csv", ".xlsx")


class BomFileError(ValueError):
    pass


def _check(lines: list[dict], path: Path) -> None:
    from .protocol import Protocol, _lint
    pr = Protocol(path=path, data={"steps": [], "bom": lines, "capabilities": []}, issues=[], files=[])
    _lint(pr, False)
    errs = [i for i in pr.issues if i.severity == "error"]
    if errs:
        raise BomFileError(f"{path.name}: " + "; ".join(f"line {i.line}: {i.msg}" for i in errs[:5]))


def _from_yaml(path: Path) -> list[dict]:
    from .protocol import _load_yaml
    issues: list = []
    doc = _load_yaml(path, issues)
    if doc is None:
        raise BomFileError("; ".join(str(i) for i in issues) or f"{path.name}: not readable")
    bom = doc.get("bom")
    if not isinstance(bom, list) or not bom:
        raise BomFileError(f"{path.name}: no `bom:` list at the top level")
    out = []
    for b in bom:
        if isinstance(b, dict):
            b["_file"], b["_line"] = str(path), getattr(b, "line", 0)
        out.append(b)
    return out


def _from_sheet(path: Path) -> list[dict]:
    from .assembly_scanner.bom import load_bom
    try:
        bom = load_bom(str(path))
    except Exception as e:
        raise BomFileError(f"{path.name}: {e}")
    out = []
    n = 0
    for ln in bom.lines:
        if not ln.designators:          # an extra item (no placement): not an assembly line
            continue
        n += 1
        bc = [x for x in (ln.mouser_pn, ln.mpn) if x]
        out.append({"line": n, "order": n, "designators": list(ln.designators), "mpn": ln.mpn or ln.mouser_pn,
                    "value": "", "package": ln.package or "", "dnf": not ln.fitted, "barcode": bc,
                    "note": ln.description or "", "_file": str(path), "_line": ln.row})
    if not out:
        raise BomFileError(f"{path.name}: no BOM line with designators")
    return out


def load(path: str | Path) -> tuple[list[dict], dict]:
    p = Path(str(path)).expanduser()
    if not p.is_file():
        raise BomFileError(f"no such file: {p}")
    p = p.absolute()                # every line's _file is absolute (bom_source, reveal), whatever the caller's cwd
    ext = p.suffix.lower()
    if ext not in FORMATS:
        raise BomFileError(f"{p.name}: {ext or 'no extension'} is not a BOM format Polybench reads ({', '.join(FORMATS)})")
    try:
        lines = _from_yaml(p) if ext in (".yaml", ".yml") else _from_sheet(p)
        _check(lines, p)
        from .protocol import _plain
        lines = _plain(lines)
        return lines, {"path": str(p.resolve()), "format": ext.lstrip("."), "size_bytes": p.stat().st_size}
    except BomFileError:
        raise
    except (OSError, UnicodeError, ValueError) as e:      # a file that is not what its extension says, or not readable
        raise BomFileError(f"{p.name}: cannot be read as a {ext.lstrip('.')} BOM ({type(e).__name__}: {e})")
