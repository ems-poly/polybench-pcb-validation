# Ported from the earlier voice prototype prg/audio.py (read-only PoC). Changes for Polybench: none yet.
"""Microphone capture with a short pre-roll so the first syllable after a pedal press is not lost."""
from __future__ import annotations

import collections
import threading
import time

from polybench.extras import lazy as _lazy
np = _lazy("numpy", "voice")

SR = 16000


STALL_S = 2.0            # no audio block for this long on a stream that has delivered before: treated as stalled
START_S = 5.0            # a stream that has not delivered its first block yet gets longer (cold CoreAudio, permission dialog)
DEVICE_POLL_S = 2.0
OPEN_WAIT_S = 0.8        # how long the caller waits for the first open before it carries on (a failure inside it is still raised)
CLOSE_WAIT_S = 0.3       # how long close() waits for the reaper before it returns anyway
STALLING_N, STALLING_WINDOW_S = 3, 60.0     # more than 3 restarts within a minute: "Microphone stream keeps stalling"


def _default_input_name(sd, device):
    """Name of the default input device, or None when the input is pinned to a device (nothing to follow).
    PortAudio caches its device list, so this sees a change only after a re-initialisation; the stall watchdog is the backstop."""
    if device is not None:
        return None
    try:
        return str(sd.query_devices(kind="input")["name"])
    except Exception:
        return None


class Recorder:
    """Microphone with a pre-roll. CoreAudio can wedge a stream: the callback stops and stop()/close() then block forever inside
    AudioOutputUnitStop. So no thread that serves a request ever calls stop/abort/close or open on a stream directly:
    opens run on a worker, stops on a daemon reaper thread that is allowed to hang for ever, and check() (called by the owner's
    watchdog) replaces a stream whose callback has stopped or whose default input device changed."""

    def __init__(self, device=None, preroll_s: float = 0.4, max_s: float = 30.0, stream_factory=None, default_input=None,
                 on_event=None, now=time.monotonic):
        if stream_factory is None:
            import sounddevice as sd
            self._sd = sd
            stream_factory = lambda **kw: sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=512, **kw)
            default_input = default_input or (lambda: _default_input_name(sd, device))
        self._factory = stream_factory
        self._default_input = default_input or (lambda: None)
        self.on_event = on_event
        self._now = now
        self.device = device
        self.pre = collections.deque(maxlen=int(preroll_s * SR / 512) + 1)
        self.buf: list[np.ndarray] = []
        self.rec = False
        self.max_blocks = int(max_s * SR / 512)
        self.lock = threading.Lock()
        self.level = 0.0
        self.tap = None                      # callable(block) for hands-free VAD; runs in the audio thread
        self.blocks = 0                      # callbacks seen (a stalled device shows as a count that stops)
        self.restarts = 0
        self.restart_times: collections.deque = collections.deque(maxlen=64)
        self.reaping = 0                     # streams handed to a reaper and not yet gone (a hung one stays counted)
        self.error = ""                      # last failed open
        self.device_name = None
        self.stream = None                   # the live stream; None while (re)opening
        self._swap = threading.Lock()        # guards stream / _gen / timestamps; never held across a PortAudio call
        self._gen = 0
        self._closed = False
        self._got_block = False
        self._last_block = now()
        self._last_probe = now()
        self._probing = False
        self._open(wait=True)

    # ---- the audio thread -----------------------------------------------------------------------
    def _make_cb(self, gen: int):
        def cb(indata, frames, t, status):
            if gen != self._gen:             # a stream that was replaced: ignore whatever it still delivers
                return
            self._last_block = self._now()
            self._got_block = True
            self._cb(indata, frames, t, status)
        return cb

    def _cb(self, indata, frames, t, status):
        blk = indata[:, 0].copy()
        self.level = float(np.sqrt(np.mean(blk ** 2)))
        self.blocks += 1
        if self.tap is not None:
            try:
                self.tap(blk)
            except Exception:
                pass
        with self.lock:
            if self.rec:
                if len(self.buf) < self.max_blocks:
                    self.buf.append(blk)
            else:
                self.pre.append(blk)

    # ---- open / reap / restart ------------------------------------------------------------------------
    def _open(self, wait: bool = False):
        """Open a stream on a worker thread. The first open waits OPEN_WAIT_S so a plain failure (no device, no permission)
        is raised to the caller; a slow one carries on in the background and the watchdog covers it."""
        with self._swap:
            self._gen += 1
            gen = self._gen
            self._last_block = self._now()
            self._got_block = False
        done = threading.Event()
        out: dict = {}

        def work():
            try:
                name = self._default_input()
                s = self._factory(device=self.device, callback=self._make_cb(gen))
                s.start()
            except Exception as e:
                out["err"] = e
                if gen == self._gen:
                    self.error = f"{type(e).__name__}: {e}"
                done.set()
                return
            with self._swap:
                keep = gen == self._gen and not self._closed
                if keep:
                    self.stream, self.device_name, self.error = s, name, ""
            if not keep:
                self._reap(s)
            done.set()
        threading.Thread(target=work, daemon=True, name="mic-open").start()
        if wait and done.wait(OPEN_WAIT_S) and "err" in out:
            self._closed = True
            raise out["err"]

    def _reap(self, stream, wait: float = 0.0):
        """Stop and close a stream on a daemon thread. It may never come back; nothing waits for it."""
        if stream is None:
            return None

        def work():
            try:
                for fn in ("abort", "close"):
                    try:
                        getattr(stream, fn)()
                    except Exception:
                        pass
            finally:
                with self._swap:
                    self.reaping -= 1
        with self._swap:
            self.reaping += 1
        t = threading.Thread(target=work, daemon=True, name="mic-reaper")
        t.start()
        if wait:
            t.join(wait)
        return t

    def _event(self, reason: str):
        print(f"voice_stream_restarted reason={reason} restarts={self.restarts}", flush=True)
        if self.on_event:
            try:
                self.on_event({"event": "voice_stream_restarted", "reason": reason, "restarts": self.restarts})
            except Exception:
                pass

    def restart(self, reason: str):
        """Replace the stream: the old one goes to a reaper, a new one opens on the current default input. Never blocks."""
        with self._swap:
            if self._closed:
                return
            old, self.stream = self.stream, None
            self.restarts += 1
            self.restart_times.append(self._now())
        self._reap(old)
        self._event(reason)
        self._open()

    def check(self):
        """Watchdog tick (any thread, never blocks): restart a stalled stream or one on a device that is no longer the default."""
        with self._swap:
            if self._closed:
                return
        now = self._now()
        limit = STALL_S if self._got_block else START_S
        if now - self._last_block > limit:
            self.restart(f"no audio block for {now - self._last_block:.1f} s")
            return
        if self.stream is not None and now - self._last_probe >= DEVICE_POLL_S and not self._probing:
            self._last_probe = now
            self._probing = True

            def probe():
                try:
                    return self._default_input()
                finally:
                    self._probing = False
            res: dict = {}
            t = threading.Thread(target=lambda: res.setdefault("n", probe()), daemon=True, name="mic-probe")
            t.start()
            t.join(0.5)
            n = res.get("n")
            if n is not None and self.device_name is not None and n != self.device_name:
                self.restart(f"default input changed: {self.device_name} -> {n}")

    # ---- status ------------------------------------------------------------------------------------
    @property
    def stream_age_s(self) -> float:
        return max(0.0, self._now() - self._last_block)

    @property
    def stalling(self) -> bool:
        now = self._now()
        return sum(1 for t in self.restart_times if now - t <= STALLING_WINDOW_S) > STALLING_N

    # ---- recording ---------------------------------------------------------------------------------
    def start(self):
        with self.lock:
            self.buf = list(self.pre)
            self.pre.clear()
            self.rec = True

    def snapshot(self) -> np.ndarray:
        """Copy of what has been recorded so far, without stopping (for the live transcript)."""
        with self.lock:
            blocks = list(self.buf)
        return np.concatenate(blocks) if blocks else np.zeros(0, dtype=np.float32)

    def stop(self) -> np.ndarray:
        with self.lock:
            self.rec = False
            a = np.concatenate(self.buf) if self.buf else np.zeros(0, dtype=np.float32)
            self.buf = []
        return a

    def cancel(self):
        self.stop()

    def close(self):
        """Returns within CLOSE_WAIT_S whatever CoreAudio does; a stalled stream is left to the reaper."""
        with self._swap:
            self._closed = True
            self._gen += 1
            old, self.stream = self.stream, None
        self._reap(old, wait=CLOSE_WAIT_S)


class FakeRecorder:
    """POLYBENCH_VOICE_TEST=1: no microphone is opened. The level is a steady 0.25 while a clip is being held, 0.01 otherwise;
    the clip itself is silence, so a held key ends as a dropped clip."""

    def __init__(self, device=None):
        self.rec = False
        self.tap = None
        self.blocks = 0
        self.restarts = 0
        self.stream_age_s = 0.0
        self.stalling = False
        self.reaping = 0

    def check(self):
        pass

    @property
    def level(self) -> float:
        return 0.25 if self.rec else 0.01

    def start(self):
        self.rec = True

    def snapshot(self) -> np.ndarray:
        return np.zeros(0, dtype=np.float32)

    def stop(self) -> np.ndarray:
        self.rec = False
        return np.zeros(0, dtype=np.float32)

    def cancel(self):
        self.rec = False

    def close(self):
        self.rec = False
