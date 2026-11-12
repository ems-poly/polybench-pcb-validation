import math

import pytest

from polybench.plugins._util import ParamError
from polybench.plugins.can_common import (BusHandle, SignalSet, SigSpec, decode_signal, encode_signals, pack_raw,
                                          series_stats, unpack_raw)

def test_pack_unpack_roundtrip_both_orders():
    for little in (True, False):
        for start, length in ((7, 12), (0, 8), (20, 16), (39, 9)) if not little else ((0, 8), (3, 13), (20, 24), (40, 24)):
            buf = bytearray(8)
            v = (1 << length) - 3
            pack_raw(buf, start, length, little, v)
            assert unpack_raw(bytes(buf), start, length, little, False) == v


def test_matches_cantools(dbc):
    import cantools
    db = cantools.database.load_file(dbc)
    sigs = SignalSet.from_params({"dbc": dbc, "signals": ["Bridge", "Temp", "Cnt", "Mot", "Mot2"]})
    for data in (bytes([0x12, 0xFF, 0xFF, 0x34, 0x12, 0x07, 0, 0]), bytes([0x80, 0x00, 0x80, 0xFE, 0x03, 0xFF, 1, 2]),
                 bytes(range(1, 9))):
        for msg in db.messages:
            ref = msg.decode(data, decode_choices=False, scaling=True)
            for s in sigs.by_id[(msg.frame_id, msg.is_extended_frame)]:
                assert decode_signal(s, data)[1] == pytest.approx(ref[s.name]), (s.name, data.hex())


def test_encode_decode_physical(dbc):
    sigs = SignalSet.from_params({"dbc": dbc, "signals": ["Bridge", "Temp"]})
    data = encode_signals(sigs.by_id[(1008, False)], {"Bridge": -12.34, "Temp": 25.0})
    got = {s.name: decode_signal(s, data)[1] for s in sigs.specs}
    assert got["Bridge"] == pytest.approx(-12.34, abs=0.006) and got["Temp"] == pytest.approx(25.0, abs=0.06)


def test_signal_errors(dbc):
    with pytest.raises(ParamError, match="not in"):
        SignalSet.from_params({"dbc": dbc, "signals": ["Nope"]})
    with pytest.raises(ParamError, match="needs a 'dbc'"):
        SignalSet.from_params({"signals": ["Bridge"]})
    with pytest.raises(ParamError, match="DBC not found"):
        SignalSet.from_params({"dbc": "/nonexistent.dbc", "signals": ["x"]})
    with pytest.raises(ParamError, match="twice"):
        SignalSet.from_params({"decode": [{"name": "a", "id": 1, "start": 0, "length": 8}] * 2})


def test_stats_math():
    s = series_stats([1.0, 2.0, 3.0, 4.0], [0.0, 0.1, 0.2, 0.3], full_scale=1024.0)
    assert s["mean"] == 2.5 and s["pp"] == 3.0
    assert s["std"] == pytest.approx(math.sqrt(5 / 3))
    assert s["rate_hz"] == pytest.approx(10.0)
    assert s["noise_free_bits"] == pytest.approx(math.log2(1024 / 3))
    assert s["effective_bits"] == pytest.approx(math.log2(1024 / s["std"]))
    flat = series_stats([5.0, 5.0, 5.0], None, 100.0)
    assert flat["noise_free_bits"] is None and flat["std"] == 0


def test_listen_only_refuses_to_transmit():
    bus = BusHandle({"listen_only": True}, True)
    with pytest.raises(Exception, match="must not transmit"):
        bus.send(0x100, b"\x00")


def test_pcan_error_messages(monkeypatch):
    import polybench.plugins.can_common as cc
    monkeypatch.setattr(cc, "pcbusb_holders", lambda: ["pid 1 (soak.py run)"])
    m = cc.explain_pcan_error(RuntimeError("A PCAN Channel has not been initialized yet or the initialization process has failed"),
                              "PCAN_USBBUS1", 1000000)
    assert "holds it" in m and "soak.py" in m and "PCAN_USBBUS1" in m
    assert "libPCBUSB" in cc.explain_pcan_error(OSError("Unable to find the library libPCBUSB.dylib"), "C", 1)
    assert "No PCAN adapter" in cc.explain_pcan_error(RuntimeError("PCAN_ERROR_NODRIVER"), "C", 1)
