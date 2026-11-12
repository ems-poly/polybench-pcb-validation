"""Live multimeter (roadmap T6.8): UNI-T UT161E over the CH9329 HID cable. See docs/MULTIMETER.md."""
from __future__ import annotations


def selftest() -> bool:
    """No hardware: framing, checksum, the captured frames, the parser, units and the stability rule."""
    from decimal import Decimal

    from . import frames as F
    from . import reading as R
    from . import units
    from .stability import StabilityWindow
    from .transport import CAPTURED

    if F.command_frame(0x5F).hex() != "abcd035f01da" or F.command_frame(0x5E).hex() != "abcd035e01d9":
        return False
    b = F.FrameBuffer()
    got = b.feed(CAPTURED["name_ack"] + CAPTURED["name"][:5])
    got += b.feed(CAPTURED["name"][5:])
    if len(got) != 2 or F.parse_name(got[1]) != "UT161E" or b.bad:
        return False
    ol = R.decode("UT161E", F.parse_measurement(CAPTURED["ohm_ol"]), 0.0)
    if ol.kind != "ol" or not ol.why_not:
        return False
    v = R.decode("UT161E", F.parse_measurement(F.build_measurement(0x02, 1, "  5.007")), 0.0)
    if v.value != Decimal("5.007") or v.unit != "V" or v.why_not or R.to_unit(v, "mV")[0] != "5007":
        return False
    bad = bytearray(CAPTURED["ohm_ol"])
    bad[6] ^= 0xFF
    b2 = F.FrameBuffer()
    if b2.feed(bytes(bad)) or b2.bad != 1:
        return False
    w = StabilityWindow(5, "0.2")
    seq = [Decimal(x) for x in ("5.007", "5.008", "5.007", "5.008", "5.007")]
    return all(not w.push(x, "c", Decimal("0.001")) for x in seq[:4]) and w.push(seq[4], "c", Decimal("0.001")) and units.quantity("kOhm") == "resistance"
