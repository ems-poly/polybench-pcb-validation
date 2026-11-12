"""Live multimeter, host-only: framing, checksum, parsing, the per-model table, conversions and the stability rule (R2-R7).
Frames are the ones captured from a UT161E on 27 Oct (brief section 3.4) plus generated ones. No hardware, no hidapi needed."""
from decimal import Decimal

import pytest

from polybench.meter import frames as F
from polybench.meter import models, units
from polybench.meter import reading as R
from polybench.meter.stability import StabilityWindow
from polybench.meter.transport import CAPTURED

OL = CAPTURED["ohm_ol"]


def rd(display="  5.007", mode=0x02, rng=1, flags=(0x30, 0x30, 0x30), model="UT161E"):
    return R.decode(model, F.parse_measurement(F.build_measurement(mode, rng, display, flags)), 0.0)


# ---- R2 frames
def test_captured_frames_check_out():
    assert F.parse_name(CAPTURED["name"]) == "UT161E"
    assert F.parse_name(CAPTURED["name_ack"]) is None
    m = F.parse_measurement(OL)
    assert (m.mode, m.range_idx, m.display) == (6, 6, "  OL.  ")
    assert F.checksum(OL[:-2]) == int.from_bytes(OL[-2:], "big")


def test_generated_dc_v_frame_matches_the_documented_fields():
    f = F.build_measurement(0x02, 1, "  5.007")
    assert len(f) == 19 and f[:3] == bytes([0xAB, 0xCD, 0x10]) and f[4] == 0x31
    m = F.parse_measurement(f)
    assert m.display == "  5.007" and m.flag_bytes == (0x30, 0x30, 0x30)


def test_commands_are_exactly_get_name_and_get_measurement():
    assert F.command_frame(0x5F).hex() == "abcd035f01da"
    assert F.command_frame(0x5E).hex() == "abcd035e01d9"
    for bad in (0x4F, 0x4A, 0x46, 0x5D, 0x00, 0xFF):             # any other command byte would press a button
        with pytest.raises(F.FrameError):
            F.command_frame(bad)


def test_bad_checksum_is_dropped_and_counted():
    bad = bytearray(OL)
    bad[-1] ^= 0x01
    b = F.FrameBuffer()
    assert b.feed(bytes(bad)) == [] and b.bad == 1
    assert b.feed(OL) == [OL]                                      # the next good frame still comes through
    with pytest.raises(F.FrameError):
        F.parse_measurement(bytes(bad))


def test_corrupted_byte_in_payload_fails_the_checksum():
    for i in range(3, 17):
        c = bytearray(OL)
        c[i] ^= 0x10
        b = F.FrameBuffer()
        assert b.feed(bytes(c)) == [] and b.bad >= 1, i


def test_split_frames_are_reassembled_and_junk_resynchronised():
    b = F.FrameBuffer()
    stream = b"\x00\x01\x02" + OL + b"\xab" + CAPTURED["name"]
    out = []
    for i in range(0, len(stream), 4):                             # arrives in 4-byte pieces
        out += b.feed(stream[i:i + 4])
    assert out == [OL, CAPTURED["name"]] and b.junk >= 3 and b.bad == 0


def test_wrong_length_frame_is_not_a_measurement():
    with pytest.raises(F.FrameError):
        F.parse_measurement(CAPTURED["name"])                      # valid frame, wrong length for a measurement
    short = F.build_frame(bytes(10))
    with pytest.raises(F.FrameError):
        F.parse_measurement(short)


def test_flag_and_range_high_nibble_checked():
    f = bytearray(F.build_measurement(2, 1, "  5.007"))
    f[4] = 0x01
    f[-2:] = F.checksum(bytes(f[:-2])).to_bytes(2, "big")
    with pytest.raises(F.FrameError):
        F.parse_measurement(bytes(f))


def test_input_report_forms():
    body = OL
    assert F.in_report_payload(bytes([19]) + body + b"\x55" * 20) == body            # bare length, stale bytes after it
    assert F.in_report_payload(bytes([0, 19]) + body) == body                        # leading report ID 0
    assert F.in_report_payload(b"") == b"" and F.in_report_payload(bytes([0])) == b""
    out = F.out_report(F.command_frame(0x5E))
    assert len(out) == 65 and out[0] == 0 and out[1] == 6 and out[2:8] == F.command_frame(0x5E)


def test_flag_bits_follow_the_fact_sheet():
    m = F.parse_measurement(F.build_measurement(2, 1, "  5.007", (0x3F, 0x3F, 0x3F)))
    f = m.flags
    assert (f.rel, f.hold, f.min, f.max, f.hv, f.low_batt, f.manual_range, f.auto_off, f.neg_bar, f.pmin, f.pmax, f.ac) == (True,) * 12
    z = F.parse_measurement(F.build_measurement(2, 1, "  5.007")).flags
    assert not any([z.rel, z.hold, z.min, z.max, z.pmin, z.pmax, z.ac])


# ---- R3 value, unit and table status
def test_value_is_parsed_from_the_display_text_as_sent():
    for text, val, dec in [("  5.007", "5.007", 3), (" -0.012", "-0.012", 3), (" 22.000", "22.000", 3), ("   1234", "1234", 0), ("  0.00 ", "0.00", 2),
                           (" 220.00", "220.00", 2)]:
        r = rd(text)
        assert r.kind == "number" and r.value == Decimal(val) and r.decimals == dec, text


def test_unit_comes_from_mode_and_range_and_status_is_marked():
    v = rd("  5.007", 0x02, 1)
    assert v.unit == "V" and v.status == "verified"                # captured on the real meter
    assert rd("  4.700", 0x06, 1).unit == "kOhm" and rd("  4.700", 0x06, 1).status == "manual"
    assert rd("  4.700", 0x06, 0).unit == "Ohm"
    assert rd("  4.700", 0x06, 5).unit == "MOhm"
    assert rd(" 12.345", 0x03, 0).unit == "mV"
    assert rd("  4.321", 0x0E, 0).unit == "mA" and rd("  4.321", 0x0C, 1).unit == "uA" and rd("  4.321", 0x10, 0).unit == "A"
    assert rd("  25.0 ", 0x0A, 0).unit == "degC"
    assert rd("  4.321", 0x02, 9).unit is None                      # range not in the table: no unit
    assert rd("  4.321", 0x02, 1, model="UT61X").unit is None       # model not in the table


def test_every_table_entry_has_a_status_and_only_captured_pairs_are_verified():
    t = models.TABLES["UT161E"]
    assert t and all(v.status in ("verified", "manual") for v in t.values())
    assert {k for k, v in t.items() if v.status == "verified"} == {(0x02, 1)}
    assert (0x04, 0) not in t                                      # frequency ladder unknown: never guessed


# ---- R4 overload
def test_overload_is_shown_and_never_a_number():
    r = R.decode("UT161E", F.parse_measurement(OL), 0.0)
    assert r.kind == "ol" and r.value is None and R.label(r) == "OL" and "overload" in r.why_not
    n = rd("  -OL. ")
    assert n.kind == "negol" and n.value is None and R.label(n) == "-OL" and n.why_not
    assert R.to_unit(n, "kOhm")[0] is None
    assert rd(" -.OL  ").kind == "negol"


def test_display_that_is_not_a_number_is_invalid():
    for t in ("  -----", "  bAtt ", "   Lo  ", "       ", "  1.2.3"):
        r = rd(t)
        assert r.kind in ("invalid",) or r.why_not, t
        assert r.value is None or r.kind == "number"


# ---- R5 not proposed
@pytest.mark.parametrize("flags,name", [((0x31, 0x30, 0x30), "REL"), ((0x32, 0x30, 0x30), "HOLD"), ((0x34, 0x30, 0x30), "MIN"),
                                         ((0x38, 0x30, 0x30), "MAX"), ((0x30, 0x30, 0x32), "P-MIN"), ((0x30, 0x30, 0x34), "P-MAX")])
def test_each_stored_value_flag_blocks_the_proposal_with_a_reason(flags, name):
    r = rd("  5.007", flags=flags)
    assert r.value == Decimal("5.007") and name in r.why_not and r.meas.flags.display() == [name]


def test_low_battery_and_range_flags_do_not_block():
    r = rd("  5.007", flags=(0x30, 0x36, 0x30))
    assert not r.why_not and r.meas.flags.low_batt and r.meas.flags.manual_range


@pytest.mark.parametrize("mode", [0x00, 0x01, 0x0D, 0x0F, 0x11, 0x15, 0x18, 0x19])
def test_ac_and_ac_dc_modes_are_shown_not_proposed(mode):
    r = rd("  3.300", mode, 1)
    assert r.ac and "AC" in r.why_not


def test_ac_flag_on_a_dc_mode_blocks():
    r = rd("  3.300", 0x02, 1, flags=(0x30, 0x30, 0x38))
    assert r.ac and r.why_not


@pytest.mark.parametrize("mode", [0x09, 0x0B, 0x12, 0x13, 0x14])
def test_modes_without_a_plan_unit_are_not_proposed(mode):
    r = rd("  3.300", mode, 0)
    assert r.why_not and "vocabulary" in r.why_not


def test_frequency_has_no_unit_until_the_ladder_is_captured():
    r = rd("  1.000", 0x04, 0)
    assert r.unit is None and "not in the" in r.why_not


# ---- R6 conversions and mismatch
@pytest.mark.parametrize("val,src,dst,out", [("5.007", "V", "mV", "5007"), ("12.345", "mV", "V", "0.012345"), ("12.345", "mV", "uV", "12345"),
                                              ("4.7", "kOhm", "Ohm", "4700"), ("4.7", "kOhm", "MOhm", "0.0047"), ("2.2", "MOhm", "kOhm", "2200"),
                                              ("0.5", "A", "mA", "500"), ("4.321", "mA", "uA", "4321"), ("220.5", "uA", "mA", "0.2205"),
                                              ("1.5", "kHz", "Hz", "1500"), ("2.5", "MHz", "kHz", "2500"), ("50.0", "%", "%", "50"),
                                              ("25.0", "degC", "degC", "25"), ("-0.012", "V", "mV", "-12"), ("0.000", "V", "V", "0")])
def test_conversion_within_a_quantity(val, src, dst, out):
    assert units.fmt(units.convert(Decimal(val), src, dst)) == out


def test_conversion_uses_the_focused_tests_unit():
    r = rd("  4.700", 0x06, 1)
    assert R.to_unit(r, "Ohm") == ("4700", "") and R.to_unit(r, "MOhm") == ("0.0047", "") and R.to_unit(r, "kOhm") == ("4.7", "")


def test_quantity_mismatch_proposes_nothing_and_says_so():
    ohm = rd("  4.700", 0x06, 1)
    v, why = R.to_unit(ohm, "V")
    assert v is None and "resistance" in why and "voltage" in why
    assert R.to_unit(rd("  5.007"), "mA")[0] is None
    assert R.to_unit(rd("  5.007"), "furlong")[0] is None
    assert R.to_unit(rd("  5.007"), "")[0] is None
    with pytest.raises(ValueError):
        units.convert(Decimal(1), "V", "A")


def test_spoken_value():
    assert units.spoken_value("5.007", "V") == "five point zero zero seven volts"
    assert units.spoken_value("-0.5", "mA") == "minus zero point five milliamps"
    assert units.spoken_value("12", "kOhm") == "twelve kilo ohms"
    assert units.spoken_value("4700", "Ohm") == "four seven zero zero ohms"


# ---- R7 stability
def win_run(vals, n=5, pct="0.2", count="0.001", ctx="c"):
    w = StabilityWindow(n, pct)
    return [w.push(Decimal(v), ctx, Decimal(count)) for v in vals]


def test_five_consecutive_readings_inside_the_band_are_stable():
    assert win_run(["5.007", "5.008", "5.007", "5.008", "5.007"]) == [False] * 4 + [True]


def test_not_stable_before_n_readings_or_with_one_outlier():
    assert win_run(["5.007"] * 4) == [False] * 4
    assert win_run(["5.007", "5.007", "5.100", "5.007", "5.007"])[-1] is False          # 1.8 % away


def test_band_is_percent_of_mean_with_a_one_count_floor():
    # 0.2 % of 5.0 is 0.010: a spread of +-0.009 is stable, +-0.012 is not
    assert win_run(["5.000", "5.009", "4.991", "5.009", "4.991"])[-1] is True
    assert win_run(["5.000", "5.012", "4.988", "5.012", "4.988"])[-1] is False
    # near zero the floor of one count decides: 0.000 / 0.001 alternate is stable (band 0.001), 0.003 is not
    assert win_run(["0.000", "0.001", "0.000", "0.001", "0.000"])[-1] is True
    assert win_run(["0.000", "0.003", "0.000", "0.003", "0.000"])[-1] is False


def test_window_slides_it_needs_the_last_n_only():
    r = win_run(["9.000", "5.000", "5.001", "5.000", "5.001", "5.000"])
    assert r[4] is False and r[5] is True


def test_mode_or_range_change_resets_the_window():
    w = StabilityWindow(5, "0.2")
    for _ in range(5):
        w.push(Decimal("5.007"), ("m", 1), Decimal("0.001"))
    assert w.stable(Decimal("0.001"))
    assert w.push(Decimal("5.007"), ("m", 2), Decimal("0.001")) is False and w.progress() == (1, 5)


def test_n_and_pct_are_configurable():
    assert win_run(["5.0", "5.01", "5.0"], n=3, count="0.1")[-1] is True
    assert win_run(["5.0", "5.2", "5.0"], n=3, pct="5", count="0.1")[-1] is True
    assert win_run(["5.0", "5.2", "5.0"], n=3, pct="0.2", count="0.1")[-1] is False
