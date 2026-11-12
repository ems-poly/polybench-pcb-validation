"""Regression guards added by the scanner agent (coordinator request, 7 Oct).

1. decode() asks zxing-cpp for Code 128 only. zxing-cpp 3.1.1's QR reader hung forever on a
   live frame (ScanWorker stuck at 100 % in ZXing::QRCode::Reader::read); the Mouser P/N is
   only ever in the Code 128 symbol (D2), so no other format may be enabled.
2. The scan watchdog abandons a hung decode and starts a fresh loop, and says so.
"""
from __future__ import annotations

import os
import threading
import time

import pytest
np = pytest.importorskip("numpy")
pytest.importorskip("cv2")
pytest.importorskip("zxingcpp")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_formats_constant_is_code128_only():
    import zxingcpp
    from polybench.assembly_scanner import scanner
    assert scanner.FORMATS == zxingcpp.BarcodeFormat.Code128


def test_decode_passes_only_code128_to_zxing(monkeypatch):
    import zxingcpp
    from polybench.assembly_scanner import scanner
    seen = []
    real = zxingcpp.read_barcodes

    def spy(img, *a, **kw):
        seen.append(kw.get("formats", a[0] if a else None))
        return real(img, *a, **kw)

    monkeypatch.setattr(scanner.zxingcpp, "read_barcodes", spy)
    scanner.decode(np.full((720, 1280, 3), 128, np.uint8))
    assert seen, "decode() never called zxing"
    assert all(f == zxingcpp.BarcodeFormat.Code128 for f in seen), seen


def test_watchdog_abandons_hung_decode():
    from polybench.assembly_scanner.service import ScanWorker       # the PoC's ScanWorker, Qt signals replaced by callbacks
    release = threading.Event()
    calls = []

    class Ctl:
        def decode_frame(self, frame):
            calls.append(threading.current_thread().name)
            if len(calls) == 1:
                release.wait(10)          # first decode "hangs"
            return []

        def step_codes(self, codes, now, run_fusion=False):
            from polybench.assembly_scanner.main import Event
            return Event(list(codes), None)

    class Cam:
        seq = 0

        def latest(self):
            Cam.seq += 1
            return Cam.seq, np.zeros((10, 10, 3), np.uint8)

    stalls = []
    w = ScanWorker(Ctl(), lambda d: None, lambda ev: None, stalls.append)
    w.STALL_S = 0.2
    w.camera = Cam()
    w.set_paused(False)
    t0 = time.monotonic()
    while not stalls and time.monotonic() - t0 < 3:
        time.sleep(0.05)
        w.check_stall()
    try:
        assert stalls and stalls[0]["count"] == 1
        t1 = time.monotonic()
        while len(calls) < 3 and time.monotonic() - t1 < 2:
            time.sleep(0.02)
        assert len(set(calls)) >= 2, "no fresh scan thread after the stall"
    finally:
        release.set()
        w.stop()
