"""Microphone stream watchdog: a CoreAudio callback that stops delivering, and a stop()/close() that then blocks for ever
(seen live: 12 threads in FinishStoppingStream -> AudioOutputUnitStop). Fake streams only: no microphone, no sound."""
import threading
import time

import numpy as np

from polybench.voice import audio as au, capture as capmod, config as vcfg


def wait(cond, t=5.0):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class FakeStream:
    """Delivers a block every 10 ms until stalled. hang_on_stop makes abort() and close() block for ever."""
    made: list = []

    def __init__(self, callback, hang_on_stop=False, **kw):
        self.cb, self.hang_on_stop = callback, hang_on_stop
        self.stalled = threading.Event()
        self.stopped = threading.Event()
        self.aborted = self.closed = False
        self.never = threading.Event()
        self.started = False
        FakeStream.made.append(self)

    def start(self):
        self.started = True
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        blk = np.full((512, 1), 0.032, np.float32)
        while not self.stalled.is_set() and not self.stopped.is_set():
            self.cb(blk, 512, None, None)
            time.sleep(0.01)

    def abort(self):
        if self.hang_on_stop:
            self.never.wait()
        self.aborted = True
        self.stopped.set()

    def close(self):
        if self.hang_on_stop:
            self.never.wait()
        self.closed = True
        self.stopped.set()


class Env:
    """Fake clock and factory. The clock is the recorder's own, so the 2 s rule is tested without waiting 2 s."""

    def __init__(self, hang=True):
        FakeStream.made = []
        self.t = 1000.0
        self.hang = hang
        self.input = "MacBook Pro Microphone"
        self.events = []

    def now(self):
        return self.t

    def factory(self, device=None, callback=None):
        return FakeStream(callback, hang_on_stop=self.hang)

    def recorder(self, **kw):
        return au.Recorder(device=None, stream_factory=self.factory, default_input=lambda: self.input, on_event=self.events.append, now=self.now, **kw)


def live(env, rec):
    """Wait until the current stream has delivered a block (blocks counter moves)."""
    n = rec.blocks
    return wait(lambda: rec.blocks > n + 2)


def test_healthy_stream_is_not_restarted():
    env = Env()
    rec = env.recorder()
    assert wait(lambda: rec.stream is not None) and live(env, rec)
    env.t += 1.0
    rec.check()
    assert rec.restarts == 0 and len(FakeStream.made) == 1
    rec.close()


def test_watchdog_fires_when_callback_stops_and_a_new_stream_opens():
    env = Env()
    rec = env.recorder()
    assert wait(lambda: rec.stream is not None) and live(env, rec)
    first = rec.stream
    first.stalled.set()                                  # the callback stops; the stream object is still "open"
    env.t += 1.5
    rec.check()
    assert rec.restarts == 0                             # under 2 s: not yet
    env.t += 1.0
    rec.check()                                          # 2.5 s without a block
    assert rec.restarts == 1
    assert wait(lambda: rec.stream is not None and rec.stream is not first)
    assert live(env, rec) and len(FakeStream.made) == 2
    assert env.events and env.events[0]["event"] == "voice_stream_restarted"
    assert rec.stream_age_s < 1.0                        # the new stream is delivering: age is back near zero
    env.t += 0.5
    rec.check()
    assert rec.restarts == 1
    rec.close()


def test_old_stream_is_handed_to_a_reaper_and_a_hanging_stop_blocks_nothing():
    env = Env(hang=True)
    rec = env.recorder()
    assert wait(lambda: rec.stream is not None) and live(env, rec)
    first = rec.stream
    first.stalled.set()
    env.t += 3.0
    t0 = time.perf_counter()
    rec.check()
    assert time.perf_counter() - t0 < 0.5                # restart() returned although abort()/close() hang
    assert wait(lambda: rec.stream is not None and rec.stream is not first)
    assert not first.aborted and not first.closed        # still hung in the reaper
    assert rec.reaping == 1
    t0 = time.perf_counter()
    rec.close()                                          # the new stream hangs too: close() still returns
    assert time.perf_counter() - t0 < 1.0
    assert rec.reaping == 2
    first.never.set()                                    # release the hung reapers so the test leaves no threads behind
    for s in FakeStream.made:
        s.never.set()
    assert wait(lambda: rec.reaping == 0)


def test_late_callbacks_of_a_replaced_stream_are_ignored():
    env = Env()
    rec = env.recorder()
    assert wait(lambda: rec.stream is not None) and live(env, rec)
    first = rec.stream
    cb = first.cb
    first.stalled.set()
    env.t += 3.0
    rec.check()
    assert wait(lambda: rec.stream is not None and rec.stream is not first)
    n = rec.blocks
    for _ in range(5):
        cb(np.ones((512, 1), np.float32), 512, None, None)
    assert rec.blocks == n or rec.blocks - n < 5         # the new stream may add its own, the stale ones add none
    assert rec.level < 0.5                               # a stale block of ones would read 1.0
    rec.close()


def test_device_change_reopens_on_the_new_default_input():
    env = Env(hang=False)
    rec = env.recorder()
    assert wait(lambda: rec.stream is not None) and live(env, rec)
    assert rec.device_name == "MacBook Pro Microphone"
    first = rec.stream
    env.t += 2.5                                         # poll interval elapsed; the stream is healthy
    env.input = "USB Audio Interface"
    rec._last_block = env.t                              # a block just arrived: only the device differs
    rec.check()
    assert rec.restarts == 1 and "USB Audio Interface" in env.events[0]["reason"]
    assert wait(lambda: rec.stream is not None and rec.stream is not first)
    assert rec.device_name == "USB Audio Interface"
    assert wait(lambda: first.closed)                    # a stop that works is simply completed by the reaper
    rec.close()


def test_pinned_device_is_not_followed():
    env = Env(hang=False)
    rec = au.Recorder(device=3, stream_factory=env.factory, default_input=lambda: None, now=env.now)
    assert wait(lambda: rec.stream is not None) and live(env, rec)
    env.t += 2.5
    rec._last_block = env.t
    rec.check()
    assert rec.restarts == 0
    rec.close()


def test_stalling_flag_after_more_than_three_restarts_in_a_minute():
    env = Env(hang=False)
    rec = env.recorder()
    assert wait(lambda: rec.stream is not None) and live(env, rec)
    for i in range(3):
        rec.restart("test")
    assert not rec.stalling
    rec.restart("test")
    assert rec.stalling and rec.restarts == 4
    env.t += 61.0
    assert not rec.stalling
    rec.close()


def test_open_that_never_returns_is_abandoned_and_retried():
    env = Env(hang=False)
    block = threading.Event()
    calls = []

    def factory(device=None, callback=None):
        calls.append(1)
        if len(calls) == 1:
            block.wait()                                 # CoreAudio wedged inside the open
        return FakeStream(callback)
    rec = au.Recorder(device=None, stream_factory=factory, default_input=lambda: None, now=env.now)    # returns after OPEN_WAIT_S
    assert rec.stream is None
    env.t += au.START_S + 1
    rec.check()
    assert wait(lambda: rec.stream is not None) and rec.restarts == 1
    block.set()                                          # the abandoned open finally returns: its stream is reaped, not installed
    installed = rec.stream
    assert wait(lambda: len(FakeStream.made) == 2)       # the retry was made first, the abandoned open's stream second
    late = [s for s in FakeStream.made if s is not installed][0]
    assert wait(lambda: late.stopped.is_set())
    assert rec.stream is installed and not installed.stopped.is_set()
    rec.close()


def test_failed_first_open_is_raised():
    def factory(device=None, callback=None):
        raise OSError("no input device")
    try:
        au.Recorder(device=None, stream_factory=factory)
    except OSError as e:
        assert "no input device" in str(e)
    else:
        raise AssertionError("expected OSError")


# ---- through Capture: enable(False) and status ------------------------------------------------------------------------
def make_cap(tmp_path, env):
    def factory(device):
        return env.recorder()
    cap = capmod.Capture(lambda: dict(vcfg.DEFAULTS), lambda: False, lambda *a: {}, lambda *a: {}, cache_dir=str(tmp_path), recorder_factory=factory)
    return cap


def test_disable_returns_within_a_second_with_a_stalled_hung_stream(tmp_path):
    env = Env(hang=True)
    cap = make_cap(tmp_path, env)
    cap.set_enabled(True)
    cap.set_visible(True)
    rec = cap.rec
    assert wait(lambda: rec.stream is not None) and live(env, rec)
    FakeStream.made[0].stalled.set()
    t0 = time.perf_counter()
    cap.set_enabled(False)
    assert time.perf_counter() - t0 < 1.0 and cap.rec is None
    t0 = time.perf_counter()
    cap.set_enabled(True)                                # and on again straight after
    cap.set_handsfree(True)
    cap.set_visible(False)
    assert time.perf_counter() - t0 < 1.0
    for s in FakeStream.made:
        s.never.set()


def test_capture_watchdog_restarts_and_status_fields(tmp_path, monkeypatch):
    env = Env(hang=True)
    cap = make_cap(tmp_path, env)
    cap.set_enabled(True)
    cap.set_visible(True)
    rec = cap.rec
    assert wait(lambda: rec.stream is not None) and live(env, rec)
    info = cap.stream_info()
    assert info["restarts"] == 0 and info["stream_age_s"] is not None and not info["stalling"]
    FakeStream.made[0].stalled.set()
    env.t += 2.5
    cap._check_stream()
    assert cap.stream_info()["restarts"] == 1
    assert wait(lambda: rec.stream is not None and rec.stream is FakeStream.made[1])
    for _ in range(4):
        rec.restart("test")
    assert cap.stream_info()["stalling"] and cap.stream_info()["restarts"] == 5
    cap.set_enabled(False)
    assert cap.stream_info() == {"stream_age_s": None, "restarts": 5, "stalling": False}     # the counter survives voice off
    for s in FakeStream.made:
        s.never.set()


def test_controller_status_carries_stream_fields(vc):
    s = vc.status()
    assert s["stream_age_s"] is None and s["restarts"] == 0 and s["stalling"] is False       # voice off: no stream wanted
    vc.enable(True)
    vc.set_visible(True)
    s = vc.status()
    assert s["listening"] and s["restarts"] == 0 and s["stalling"] is False and s["stream_age_s"] == 0.0
    vc.enable(False)
