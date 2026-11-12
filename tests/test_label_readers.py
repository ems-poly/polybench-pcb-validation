"""Label readers (UIR-47 S-1..S-3). All strings are synthetic DEMO strings built from public format notes; nothing here
was scanned from a real label. The DigiKey and LCSC readers are UNVALIDATED."""
import importlib.metadata as md

import pytest

from polybench import label_readers as lr

RS, GS, EOT = "\x1e", "\x1d", "\x04"


def env(*fields):
    return "[)>" + RS + "06" + "".join(GS + f for f in fields) + RS + EOT


MOUSER_ENV = env("PDEMO1 R3,4,5", "1PRC0402FR-0710KL", "Q100", "1TLOT42")
DIGIKEY_ENV = env("PDEMO1 R3,4,5", "1PRC0402FR-0710KL", "30PDEMO-5032-1-ND", "K PO-1", "1K00000001", "10K00000002",
                  "9D2540", "1TLOT42", "4LXX", "Q100", "11Z" + "PICK", "20Z0000")


def test_s1_entry_point_group_registers_the_builtins():
    names = {e.name for e in md.entry_points(group="polybench.label_readers")}
    assert {"mouser", "digikey", "lcsc"} <= names
    assert {r.name for r in lr.readers(rescan=True)} >= {"mouser", "digikey", "lcsc"}


def test_s1_interface_keys_on_every_reader():
    for r in lr.readers():
        got = r.parse("not a label at all")
        assert got is None or {"mpn", "qty", "vendor_pn", "lot"} <= set(got)


def test_s1_mouser_plain_pn_through_the_interface():
    got = lr.read_label("603-RC0402FR-0710KL")
    assert got["vendor"] == "mouser" and got["vendor_pn"] == "603-RC0402FR-0710KL" and got["mpn"] is None


def test_s1_mouser_envelope_gives_mpn_qty_lot_and_drops_customer_pn():
    got = lr.MouserReader().parse(MOUSER_ENV)
    assert (got["mpn"], got["qty"], got["lot"]) == ("RC0402FR-0710KL", 100, "LOT42")
    assert "DEMO1" not in repr(got)


def test_s1_a_failing_reader_is_skipped(monkeypatch):
    class Boom:
        name = "boom"
        def parse(self, raw):
            raise RuntimeError("x")
    monkeypatch.setitem(lr._cache, "r", [Boom(), lr.MouserReader()])
    assert lr.read_label("603-RC0402FR-0710KL")["vendor"] == "mouser"


def test_s1_an_added_reader_is_used(monkeypatch):
    class Demo(lr.LabelReader):
        name = "demo"
        def parse(self, raw):
            return lr._result("demo", mpn=raw[5:]) if raw.startswith("DEMO:") else None
    monkeypatch.setitem(lr._cache, "r", [Demo()])
    assert lr.read_label("DEMO:ABC")["mpn"] == "ABC" and lr.read_label("other") is None


def test_s2_digikey_envelope():
    got = lr.read_label(DIGIKEY_ENV)
    assert got["vendor"] == "digikey" and got["vendor_pn"] == "DEMO-5032-1-ND" and got["mpn"] == "RC0402FR-0710KL"
    assert got["qty"] == 100 and got["lot"] == "LOT42" and got["date_code"] == "2540" and got["country"] == "XX"


def test_s2_digikey_30p_without_order_fields_is_not_taken_as_a_part_number():
    """A supplier spec uses 30P for RoHS (Y/N): without 1K or 10K it is not read as a DigiKey part number."""
    assert lr.DigikeyReader().parse(env("1PRC0402FR-0710KL", "30PY", "Q5")) is None


def test_s2_digikey_plain_nd_pn_and_mouser_does_not_claim_it():
    assert lr.read_label("732-8660-1-ND")["vendor"] == "digikey"
    assert lr.MouserReader().parse("732-8660-1-ND") is None


def test_s2_lcsc_is_a_stub_that_reads_nothing():
    r = lr.LcscReader()
    assert r.status == "STUB" and r.parse("anything") is None and r.parse("") is None
    assert next(x for x in lr.reader_status() if x["name"] == "lcsc")["status"] == "STUB"


def test_labels_expand_uses_the_readers():
    from polybench import labels
    cands, qty = labels.expand([DIGIKEY_ENV])
    assert cands == ["RC0402FR-0710KL", "DEMO-5032-1-ND"] and qty == 100


# ---- S-3 miss records in the journal
def last(engine):
    return engine.journal.events[-1]["payload"]


def test_s3_no_mpn(engine):
    engine.scan("", via="camera", codes=[env("PDEMO1 R3,4,5", "Q5")])
    assert last(engine)["miss"]["reason"] == "no_mpn" and engine.state.alog[-1]["miss"] == "no_mpn"


def test_s3_not_in_bom(engine):
    engine.scan("zzz", via="camera", codes=["999-NOPE"])
    assert last(engine)["miss"]["reason"] == "not_in_bom" and engine.state.alog[-1]["kind"] == "scan_miss"


def test_s3_ambiguous_keeps_first_match(engine):
    r = engine.scan("", codes=["603-RC0402FR-0710KL", "595-DEMOMCU32"])
    assert r["ok"] and r["line"] == 5
    assert last(engine)["miss"]["reason"] == "ambiguous" and "1" in last(engine)["miss"]["detail"]


def test_s3_quantity_mismatch(engine):
    r = engine.scan("", codes=[env("1PRC0402FR-0710KL", "Q5")])          # line 5 needs 3 x 3 boards = 9
    assert r["ok"] and r["line"] == 5 and last(engine)["miss"]["reason"] == "quantity_mismatch"


def test_s3_clean_match_has_no_miss(engine):
    engine.scan("", codes=[env("1PRC0402FR-0710KL", "Q100")])
    assert "miss" not in last(engine)


def test_s3_miss_survives_a_journal_replay(engine, tmp_path):
    from helpers import reopen
    engine.scan("zzz", codes=["999-NOPE"])
    e2 = reopen(engine, tmp_path / "j.jsonl")
    assert e2.state.alog[-1]["miss"] == "not_in_bom"
