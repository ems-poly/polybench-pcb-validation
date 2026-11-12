"""`polybench doctor` meter line and the selftest (R14, R16). Host-only: hidapi and the cable are faked."""
import types

from polybench import doctor, meter
from polybench.meter import transport as T
from polybench.meter.transport import SimTransport


def fake_cable(monkeypatch, present=True, transport=None):
    monkeypatch.setattr(T, "_hid", lambda: types.SimpleNamespace())
    monkeypatch.setattr(T, "find_cable", lambda: [{"path": b"x", "product": "WCH UART TO KB-MS_V1.8", "serial": "1"}] if present else [])
    if transport is not None:
        monkeypatch.setattr(T, "HidTransport", lambda: transport)


def test_selftest_covers_parser_and_stability_without_hardware():
    assert meter.selftest() is True


def test_found_with_model_string(monkeypatch):
    fake_cable(monkeypatch, True, SimTransport(latency_s=0))
    (c,) = doctor.check_meter()
    assert c.status == doctor.OK and "UT161E" in c.detail and "WCH UART" in c.detail


def test_absent_is_info_not_an_error_unless_the_protocol_asks(monkeypatch):
    fake_cable(monkeypatch, False)
    assert doctor.check_meter()[0].status == doctor.INFO
    assert doctor.check_meter(needed=True)[0].status == doctor.FAIL
    assert doctor.check_meter(needed=True, severity=doctor.WARN)[0].status == doctor.WARN


def test_hidapi_missing_says_how_to_install(monkeypatch):
    def boom():
        raise T.NotInstalled("hidapi is not installed")
    monkeypatch.setattr(T, "_hid", boom)
    (c,) = doctor.check_meter()
    assert c.status == doctor.INFO and ".[dmm]" in c.hint
    assert doctor.check_meter(needed=True)[0].status == doctor.FAIL


def test_no_open_does_not_touch_the_device(monkeypatch):
    fake_cable(monkeypatch, True, None)
    monkeypatch.setattr(T, "HidTransport", lambda: (_ for _ in ()).throw(AssertionError("opened")))
    (c,) = doctor.check_meter(probe=False)
    assert "not opened" in c.detail


def test_cable_held_by_another_program(monkeypatch):
    class Busy(SimTransport):
        def open(self):
            raise T.TransportError("cannot open the cable (open failed)")
    fake_cable(monkeypatch, True, Busy())
    (c,) = doctor.check_meter()
    assert c.status == doctor.WARN and "holds" in c.hint


def test_meter_asleep(monkeypatch):
    class Mute(SimTransport):
        def write(self, uart):
            pass
    fake_cable(monkeypatch, True, Mute(latency_s=0))
    (c,) = doctor.check_meter()
    assert c.status == doctor.WARN and "did not answer" in c.detail


def test_run_checks_includes_the_meter_line(monkeypatch):
    fake_cable(monkeypatch, False)
    res = doctor.run_checks(None, open_pcan=False, port=59999)
    assert any(c.name == "multimeter" for c in res)
