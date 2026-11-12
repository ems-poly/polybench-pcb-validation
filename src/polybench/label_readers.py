"""Vendor label readers: one interface, an entry-point group, one reader per distributor.

A reader turns the text of one decoded barcode into what the label says:

    reader.parse(raw: str) -> {"mpn": str|None, "qty": int|None, "vendor_pn": str|None, "lot": str|None, ...} | None

`None` means "this is not my label". A reader never matches against the BOM and never decides a verdict; it only reads.
The customer part number (ECIA field `P`) is never returned: it is shortened by the buyer on purpose (CONTRACTS D7).

Registering: `[project.entry-points."polybench.label_readers"]  name = "pkg.module:ReaderClass"`. The registry instantiates the
class with no arguments. Built-ins below are always present; an entry point with the same name replaces one. A reader that
fails to load or raises in `parse` is skipped and reported by `reader_status()`, it never stops a scan.

Labels of the readers here:
  mouser   UNVALIDATED  the Mouser P/N Code 128 shape the scanner already used (TBR-1), plus the ECIA envelope
  digikey  UNVALIDATED  public DigiKey format notes; no real label has been scanned
  lcsc     stub, NOT MET  no public format documentation was found; see LcscReader
"""
from __future__ import annotations

import importlib.metadata as md
import re

from polybench import labels

GROUP = "polybench.label_readers"
_MOUSER_SHAPE = re.compile(r"^\d{2,3}-\S+$")      # same shape as assembly_scanner.bom.MOUSER_PN_SHAPE (TBR-1)


def _result(vendor: str, **kw) -> dict:
    out = {"vendor": vendor, "mpn": None, "qty": None, "vendor_pn": None, "lot": None}
    out.update(kw)
    return out


def _qty(v) -> int | None:
    return int(v) if v and re.fullmatch(r"\d{1,7}", str(v)) else None


class LabelReader:
    """Base class. Subclass, set `name` and `status`, implement `parse`."""
    name = ""
    status = "UNVALIDATED"      # HARDWARE-VALIDATED only after real labels were scanned and compared

    def parse(self, raw: str) -> dict | None:
        raise NotImplementedError


class MouserReader(LabelReader):
    """Mouser. A plain Code 128 is the Mouser P/N (`\\d{2,3}-...`, the shape the scanner already relied on, TBR-1).
    An ECIA/ISO 15434 envelope without a DigiKey field gives 1P (MPN), Q (quantity), 1T (lot) (see labels.parse_label).
    Not read here: the customer part number. No Mouser label specification was consulted for this class: UNVALIDATED."""
    name = "mouser"

    def parse(self, raw):
        text = str(raw or "").strip()
        if not text:
            return None
        info = labels.parse_label(text)
        if info["envelope"]:
            f = info["fields"]
            if "30P" in f or not (f.get("1P") or info["qty"]):
                return None
            return _result("mouser", mpn=f.get("1P"), qty=info["qty"], lot=f.get("1T"))
        if _MOUSER_SHAPE.match(labels.norm(text)) and not text.upper().endswith("-ND"):
            return _result("mouser", vendor_pn=labels.norm(text))
        return None


class DigikeyReader(LabelReader):
    """DigiKey, from the public notes "DigiKey Product Labels - Decoding DigiKey Barcodes"
    (DigiKey community forum, topic 41097, read 2026-11-12), which rest on
    ECIA EIGP 114 / ANSI MH10.8.2 and ISO/IEC 15434:
      envelope  `[)>` RS `06` GS, fields separated by GS (0x1D)
      30P  DigiKey part number     1P  manufacturer part number     K  customer PO     1K  sales order
      10K  invoice     9D  date code     1T  lot code     11K  pack list     4L  country of origin     Q  quantity
      P  customer part number (ignored here)     11Z/12Z/13Z/20Z  internal and padding (ignored)
    A plain Code 128 DigiKey part number ends in `-ND` (the notes' examples: `732-8660-1-ND`, `Q1045-ND`).
    Caveat found while reading: another public spec (a supplier label spec) uses 30P for RoHS, so 30P is taken as the DigiKey
    part number only together with the DigiKey sales order 1K or invoice 10K field. UNVALIDATED: no real DigiKey label was
    scanned, and the sample strings in the notes are truncated."""
    name = "digikey"

    def parse(self, raw):
        text = str(raw or "").strip()
        if not text:
            return None
        info = labels.parse_label(text)
        if info["envelope"]:
            f = info["fields"]
            if not f.get("30P") or not (f.get("1K") or f.get("10K")):
                return None
            return _result("digikey", mpn=f.get("1P"), qty=info["qty"], vendor_pn=f["30P"], lot=f.get("1T"),
                           date_code=f.get("9D"), country=f.get("4L"), sales_order=f.get("1K"))
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.,/+-]*-ND", text, re.I):
            return _result("digikey", vendor_pn=text.upper())
        return None


class LcscReader(LabelReader):
    """LCSC: STUB, requirement not met. A search for public LCSC label or QR format documentation (2026-11-12) found none, and
    no format is invented here. To finish it: obtain LCSC's own format description or real sample labels from an order, write
    the parser in `parse`, add synthetic strings to tests/test_label_readers.py that cite the source, and change `status`."""
    name = "lcsc"
    status = "STUB"

    def parse(self, raw):
        return None


BUILTIN: dict[str, type] = {"mouser": MouserReader, "digikey": DigikeyReader, "lcsc": LcscReader}
_cache: dict = {}


def _load() -> tuple[list[LabelReader], dict[str, str]]:
    readers: dict[str, LabelReader] = {n: c() for n, c in BUILTIN.items()}
    errors: dict[str, str] = {}
    try:
        eps = list(md.entry_points(group=GROUP))
    except Exception:
        eps = []
    for ep in eps:
        try:
            obj = ep.load()
            obj = obj() if isinstance(obj, type) else obj
            if not callable(getattr(obj, "parse", None)):
                raise TypeError("no parse(raw) method")
            readers[ep.name] = obj
        except Exception as e:
            errors[ep.name] = f"{type(e).__name__}: {e}"
    return list(readers.values()), errors


def readers(rescan: bool = False) -> list[LabelReader]:
    if rescan or "r" not in _cache:
        _cache["r"], _cache["e"] = _load()
    return list(_cache["r"])


def reader_status() -> list[dict]:
    readers()
    rows = [{"name": getattr(r, "name", "?"), "status": getattr(r, "status", "UNVALIDATED"), "error": ""} for r in _cache["r"]]
    rows += [{"name": n, "status": "FAILED", "error": e} for n, e in _cache["e"].items()]
    return rows


def read_label(raw: str) -> dict | None:
    """The first reader that recognises `raw`. A reader that raises is skipped."""
    for r in readers():
        try:
            got = r.parse(raw)
        except Exception:
            continue
        if got:
            got.setdefault("vendor", getattr(r, "name", ""))
            return got
    return None
