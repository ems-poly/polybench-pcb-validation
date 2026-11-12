# Ported from the voice proof of concept. Changes for Polybench: imports; board-word tests added in test_understand.
import pytest
from polybench.voice.grammar import IdIndex, parse, parse_number, _tokens

IDS = ["C07", "C08", "C09", "C10", "V02", "V07", "V07a", "P19", "R6", "SENS_G", "P09a", "P21b", "C14a", "C14k",
       "PV02", "PI16", "CAN_LB_XCVR", "ripple_pp_mv_180ma", "ripple_pp_mv_1ma", "ripple_c21_mv", "d3_drop_100ma",
       "d3_drop_180ma", "fsw_khz", "fsw_span_khz", "iin_5v", "iin_9v", "P19a", "P19b", "HEAP", "SENS_ID"]
IDX = IdIndex(IDS)


def num(s):
    toks, _ = _tokens(s)
    r = parse_number(toks, 0)
    return None if r is None else (str(r[0]), r[1] == len(toks))


@pytest.mark.parametrize("s,v", [
    ("412", "412"), ("four hundred twelve", "412"), ("four hundred and twelve", "412"), ("twenty two", "22"),
    ("three point three one", "3.31"), ("3.31", "3.31"), ("zero point four five", "0.45"), ("point five", "0.5"),
    ("two thousand", "2000"), ("one thousand two hundred", "1200"), ("minus point two", "-0.2"),
    ("one hundred twenty three", "123"), ("nineteen", "19"), ("four one two", "412"), ("twenty two point zero one", "22.01"),
    ("12", "12"), ("-0.5", "-0.5"), ("1,200", "1200"),
])
def test_numbers(s, v):
    assert num(s) == (v, True)


def val(text):
    r = parse(text, IDX)
    assert r["ok"], r
    return r


@pytest.mark.parametrize("text,unit,number", [
    ("C07 412 ohm", "Ω", "412"), ("C07 412 ohms", "Ω", "412"), ("C07 412Ω", "Ω", "412"), ("C07 4.7 kΩ", "kΩ", "4.7"),
    ("C07 4.7 kilo ohms", "kΩ", "4.7"), ("C07 four point seven kilohms", "kΩ", "4.7"), ("C07 1.2 megohms", "MΩ", "1.2"),
    ("C07 1.2 mega ohm", "MΩ", "1.2"), ("C07 3 milliohms", "mΩ", "3"), ("C08 0.45 V", "V", "0.45"),
    ("C08 zero point four five volts", "V", "0.45"), ("C08 450 millivolts", "mV", "450"), ("C08 450 mV", "mV", "450"),
    ("C08 450mV", "mV", "450"), ("C08 22 milliamps", "mA", "22"), ("C08 5 microamps", "µA", "5"),
    ("C08 2.2 megahertz", "MHz", "2.2"), ("C08 2100 kilohertz", "kHz", "2100"), ("C08 4.5 milliseconds", "ms", "4.5"),
    ("C08 20 microseconds", "µs", "20"), ("C08 12 percent", "%", "12"), ("C08 12%", "%", "12"),
])
def test_units(text, unit, number):
    v = val(text)["value"]
    assert (v["kind"], v["number"], v["unit"]) == ("number", number, unit)


@pytest.mark.parametrize("text,rid", [
    ("V07a OK", "V07a"), ("V07 OK", "V07"), ("V 0 7 a OK", "V07a"), ("vee zero seven a OK", "V07a"),
    ("C07 OK", "C07"), ("see oh seven OK", "C07"), ("C-07 OK", "C07"), ("P19 OK", "P19"), ("pee nineteen OK", "P19"),
    ("P one nine OK", "P19"), ("R6 OK", "R6"), ("are six OK", "R6"), ("SENS_G OK", "SENS_G"), ("sens G OK", "SENS_G"),
    ("sens gee OK", "SENS_G"), ("C fourteen a OK", "C14a"), ("C one four k OK", "C14k"), ("P V zero two OK", "PV02"),
    ("PI sixteen OK", "PI16"), ("P09a OK", "P09a"), ("p21b OK", "P21b"), ("CAN LB XCVR OK", "CAN_LB_XCVR"),
])
def test_ids(text, rid):
    r = val(text)
    assert r["id"] == rid and r["value"]["kind"] == "ok" and not r["id_candidates"]


def test_p19_vs_variants():
    assert val("P19 OK")["id"] == "P19"
    assert val("P19a OK")["id"] == "P19a"
    assert val("P nineteen b OK")["id"] == "P19b"


@pytest.mark.parametrize("text,rid,cands", [
    ("ripple one eighty 45 millivolts", "ripple_pp_mv_180ma", None),
    ("ripple one 45 millivolts", "ripple_pp_mv_1ma", None),
    ("d3 drop one hundred 2 volts", "d3_drop_100ma", None),
    ("iin five volt 12 milliamps", "iin_5v", None),
    ("fsw khz 2200 kilohertz", "fsw_khz", None),
])
def test_spoken_alias(text, rid, cands):
    r = val(text)
    assert r["id"] == rid and r["id_candidates"] is cands


def test_alias_ambiguous_is_flagged_not_guessed():
    r = parse("ripple 45 millivolts", IDX)
    assert r["id_candidates"] and len(r["id_candidates"]) >= 3


def test_boards():
    assert val("board three C07 412 ohm")["boards"] == [3]
    assert val("B3 C07 412 ohm")["boards"] == [3]
    assert val("be three C07 412 ohm")["boards"] == [3]
    assert val("bee 3 C07 412 ohm")["boards"] == [3]
    assert val("all boards V02 OK")["boards"] == "all"
    assert val("C07 412 ohm board 2")["boards"] == [2]
    assert parse("board three", IDX)["intent"] == "set_board"
    assert parse("fail boards two and four", IDX)["boards"] == [2, 4]


def test_value_kinds():
    assert val("board 5 V02 OK")["value"] == {"kind": "ok"}
    assert val("C07 OL")["value"] == {"kind": "ol"}
    assert val("C07 over range")["value"] == {"kind": "ol"}
    assert val("V02 yes")["value"] == {"kind": "yn", "values": ["Y"]}
    assert val("V02 yes yes no")["value"] == {"kind": "yn", "values": ["Y", "Y", "N"]}
    assert val("SENS_ID hex five zero")["value"] == {"kind": "hex", "text": "0x50"}
    assert val("SENS_ID 0x50")["value"] == {"kind": "hex", "text": "0x50"}
    assert val("SENS_ID hex 5-0")["value"] == {"kind": "hex", "text": "0x50"}
    assert val("P19 text FH4R2")["value"] == {"kind": "text", "text": "FH4R2"}
    assert val("HEAP verdict pass 152000")["value"] == {"kind": "verdict", "text": "PASS 152000"}
    assert val("C07 4.7 k")["value"]["prefix"] == "k"


@pytest.mark.parametrize("text,intent", [
    ("pass", "pass"), ("undo", "undo"), ("accept", "accept"), ("skip", "skip"), ("repeat", "repeat"), ("next", "next"),
    ("Accept.", "accept"), ("accept all", "accept_all"), ("reject", "reject"), ("scratch that", "reject"),
    ("previous", "back"), ("C07", "goto"), ("row three", "goto"),
])
def test_commands(text, intent):
    assert val(text)["intent"] == intent


def test_note():
    r = val("note C07 pad lifted at U1")
    assert r["intent"] == "note" or r["intent"] == "value"


@pytest.mark.parametrize("text", ["banana", "C07 banana", "C07 412 flurbs", "C07", "board three C07 four twelve ohm extra words here",
                                  "", "C07 4.7.2 ohm"])
def test_not_understood(text):
    r = parse(text, IDX)
    assert not (r["ok"] and r["intent"] == "value"), r


def test_stt_punctuation():
    r = val("Board 3. C07. 412 ohms.")
    assert r["boards"] == [3] and r["id"] == "C07" and r["value"]["number"] == "412"
    r = val("Board three, C07, four hundred twelve ohms.")
    assert r["value"]["number"] == "412"
