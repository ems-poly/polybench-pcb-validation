"""Live multimeter reader against a fake HID device (R1, R2, R12, R13, R16). Host-only: no hidapi, no USB, no hardware."""
import time
import types

import pytest

from polybench.meter import frames as F
from polybench.meter import link, transport
from polybench.meter.transport import SimTransport, TransportError


def until(fn, t=5.0):
    end = time.monotonic() + t
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.01)
    return False


class Collect:
    def __init__(self):
        self.readings, self.states = [], 0

    def on_reading(self, r):
        self.readings.append(r)

    def on_state(self):
        self.states += 1


def reader(tr, col, finder=None, **kw):
    return link.MeterReader(lambda: tr, col.on_reading, col.on_state, finder=finder or (lambda: [{"product": "fake cable"}] if tr.plugged else []), retry_s=0.05, **kw)


def test_link_reads_the_model_once_then_measurements():
    tr = SimTransport(latency_s=0)
    tr.open()
    lk = link.MeterLink(tr)
    assert lk.get_name() == "UT161E"
    m = lk.measure()
    assert m and m.display == "  5.007" and m.mode == 2
    assert tr.sent == [0x5F, 0x5E]


def test_only_get_name_and_get_measurement_are_ever_sent():
    tr = SimTransport(latency_s=0)
    col = Collect()
    rd = reader(tr, col)
    rd.start()
    try:
        assert until(lambda: len(col.readings) >= 5)
    finally:
        rd.stop()
        rd.join(2)
    assert set(tr.sent) == {0x5E, 0x5F} and tr.sent.count(0x5F) == 1            # the name is asked once per connection


def test_reader_reports_model_and_polls_fast():
    tr = SimTransport(latency_s=0.001)
    col = Collect()
    rd = reader(tr, col)
    rd.start()
    try:
        assert until(lambda: rd.state == "connected" and len(col.readings) > 20)
        st = rd.status()
        assert st["model"] == "UT161E" and st["cable"] == "fake cable" and st["rate_hz"] > 13
    finally:
        rd.stop()
        rd.join(2)
    assert all(r.model == "UT161E" for r in col.readings)


def test_split_reports_are_reassembled():
    tr = SimTransport(latency_s=0, split=True)
    col = Collect()
    rd = reader(tr, col)
    rd.start()
    try:
        assert until(lambda: len(col.readings) >= 3)
    finally:
        rd.stop()
        rd.join(2)
    assert col.readings[0].text == "5.007" and rd.status()["frames_bad"] == 0


class Junky(SimTransport):
    """Every third measurement answer is corrupted, and every fifth is followed by a frame of the wrong length."""
    def _reply(self, frame):
        self.n = getattr(self, "n", 0) + 1
        if len(frame) == 19 and self.n % 3 == 0:
            frame = frame[:-1] + bytes([frame[-1] ^ 1])
        super()._reply(frame)
        if len(frame) == 19 and self.n % 5 == 0:
            super()._reply(F.build_frame(b"\x01\x02\x03"))


def test_bad_and_wrong_length_frames_are_dropped_counted_never_shown():
    tr = Junky(latency_s=0)
    col = Collect()
    rd = reader(tr, col)
    rd.start()
    try:
        assert until(lambda: len(col.readings) >= 10)
    finally:
        rd.stop()
        rd.join(2)
    assert rd.status()["frames_bad"] >= 3
    assert all(r.text == "5.007" for r in col.readings)                         # nothing corrupted reached a reading


def test_unplug_shows_lost_within_two_seconds_and_replug_reconnects_without_restart():
    tr = SimTransport(latency_s=0.005)
    col = Collect()
    rd = reader(tr, col)
    rd.start()
    try:
        assert until(lambda: rd.state == "connected")
        t0 = time.monotonic()
        tr.set_scene("unplugged")
        assert until(lambda: rd.state == "lost", 2.0), rd.state
        assert time.monotonic() - t0 < 2.0
        n = len(col.readings)
        time.sleep(0.2)
        assert len(col.readings) == n                                            # nothing while lost
        tr.set_scene("plugged")
        assert until(lambda: rd.state == "connected", 3.0)
        assert until(lambda: len(col.readings) > n, 3.0)
        assert rd.status()["reconnects"] >= 2
    finally:
        rd.stop()
        rd.join(2)


class Silent(SimTransport):
    def write(self, uart):
        if self.is_open:
            self.sent.append(uart[3])                                              # the meter is off: nothing comes back


def test_cable_present_but_meter_silent_is_its_own_state_then_lost_after_connect(monkeypatch):
    tr = Silent(latency_s=0.001)
    col = Collect()
    rd = reader(tr, col)
    rd.start()
    try:
        assert until(lambda: rd.state == "silent")
        assert "does not answer" in rd.reason and not col.readings
    finally:
        rd.stop()
        rd.join(2)


def test_meter_that_stops_answering_after_connecting_is_lost_in_time():
    class Dies(SimTransport):
        dead = False

        def write(self, uart):
            if self.dead:
                self.sent.append(uart[3])
                return
            super().write(uart)
    tr = Dies(latency_s=0.005)
    col = Collect()
    rd = reader(tr, col)
    rd.start()
    try:
        assert until(lambda: rd.state == "connected")
        tr.dead = True
        t0 = time.monotonic()
        assert until(lambda: rd.state == "lost", 2.5)
        assert time.monotonic() - t0 < 2.0
    finally:
        rd.stop()
        rd.join(2)


def test_no_cable_state_when_none_on_usb():
    tr = SimTransport()
    col = Collect()
    rd = link.MeterReader(lambda: tr, col.on_reading, col.on_state, finder=lambda: [], retry_s=0.02)
    rd.start()
    try:
        assert until(lambda: rd.state == "no_cable")
        assert rd.reason and "1A86:E429" in rd.reason
    finally:
        rd.stop()
        rd.join(2)


def test_hidapi_missing_is_a_state_with_the_install_hint_not_a_crash(monkeypatch):
    def boom():
        raise transport.NotInstalled("hidapi is not installed: " + transport.INSTALL_HINT)
    monkeypatch.setattr(transport, "_hid", boom)
    ok, why = transport.hid_available()
    assert not ok and "pip install -e '.[dmm]'" in why
    col = Collect()
    rd = link.MeterReader(transport.HidTransport, col.on_reading, col.on_state, retry_s=0.02)      # the real finder, hidapi absent
    rd.start()
    try:
        assert until(lambda: rd.state == "not_installed")
        assert ".[dmm]" in rd.reason
    finally:
        rd.stop()
        rd.join(2)


def test_pause_releases_the_cable_and_resume_takes_it_back():
    tr = SimTransport(latency_s=0.001)
    col = Collect()
    rd = reader(tr, col)
    rd.start()
    try:
        assert until(lambda: rd.state == "connected")
        assert rd.pause(2.0) and not tr.is_open
        n = len(col.readings)
        time.sleep(0.1)
        assert len(col.readings) == n
        rd.resume()
        assert until(lambda: tr.is_open and len(col.readings) > n)
    finally:
        rd.stop()
        rd.join(2)


def test_a_failure_inside_a_callback_does_not_kill_the_reader(caplog):
    tr = SimTransport(latency_s=0.001)
    seen = []

    def bad(r):
        seen.append(r)
        raise RuntimeError("boom")
    rd = link.MeterReader(lambda: tr, bad, lambda: None, finder=lambda: [{"product": "x"}], retry_s=0.02)
    rd.start()
    try:
        assert until(lambda: len(seen) >= 3)
    finally:
        rd.stop()
        rd.join(2)
    assert "Traceback" not in caplog.text


def test_open_failure_names_the_holder_not_a_traceback():
    class Busy(SimTransport):
        def open(self):
            raise TransportError("cannot open the cable (open failed); another program may hold it (only one process can)")
    col = Collect()
    rd = link.MeterReader(lambda: Busy(), col.on_reading, col.on_state, finder=lambda: [{"product": "x"}], retry_s=0.02)
    rd.start()
    try:
        assert until(lambda: rd.state == "no_cable" and "another program" in rd.reason)
    finally:
        rd.stop()
        rd.join(2)


def test_hid_transport_report_format_with_a_fake_hid_module(monkeypatch):
    sent = []

    class Dev:
        def open(self, v, p):
            assert (v, p) == (0x1A86, 0xE429)

        def write(self, b):
            sent.append(bytes(b))
            return len(b)

        def read(self, n, ms):
            return list(bytes([19]) + F.build_measurement(2, 1, "  5.007") + bytes(44))

        def close(self):
            pass
    fake = types.SimpleNamespace(device=Dev, enumerate=lambda v, p: [{"path": b"x", "product_string": "WCH UART TO KB-MS_V1.8", "serial_number": "1"}])
    monkeypatch.setattr(transport, "_hid", lambda: fake)
    assert transport.find_cable()[0]["product"].startswith("WCH")
    t = transport.HidTransport()
    t.open()
    t.write(F.command_frame(0x5E))
    assert len(sent[0]) == 65 and sent[0][:2] == bytes([0, 6])
    assert link.MeterLink(t).measure().display == "  5.007"
    t.close()
