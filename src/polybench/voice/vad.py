# Ported from the earlier voice prototype prg/vad.py (read-only PoC). Changes for Polybench: none yet.
"""Hands-free capture: energy voice-activity detection that cuts the microphone stream into utterances.

No model, no network. An utterance starts when a few consecutive 32 ms blocks are clearly above an adaptive noise
floor and look like speech (most energy in 300-3400 Hz, so hum, rumble and fan noise do not start one). It ends after a
silence gap (default 0.8 s, tunable). Clips with too little voiced time or a low peak are discarded (tool clicks, a door).
While the tool is speaking or playing its cue sound the caller passes muted=True: nothing is analysed, a running
utterance is dropped, and the noise floor is not updated, so the tool never transcribes itself.

Pure numpy and testable: `Segmenter.feed(block)` returns an `Utterance` when one has just ended.
"""
from __future__ import annotations

import collections
from dataclasses import dataclass

from polybench.extras import lazy as _lazy
np = _lazy("numpy", "voice")

SR = 16000
BLOCK = 512                       # 32 ms


@dataclass
class Utterance:
    audio: np.ndarray
    t_start: float                # seconds of stream time at the first sample kept
    t_speech_end: float           # last block that was above the end threshold
    t_emit: float                 # when the gap elapsed and the utterance was released
    voiced_s: float
    peak: float
    floor: float


@dataclass
class VadParams:
    gap_s: float = 0.8            # silence that ends an utterance
    min_voiced_s: float = 0.15    # less voiced time than this is a click, not speech
    min_peak: float = 0.03        # block RMS the utterance must reach at least once (room murmur stays below)
    start_ratio: float = 3.0      # start threshold = floor * ratio (about 9.5 dB over the noise)
    end_ratio: float = 2.0
    rel_end: float = 0.2          # a block only counts as speech inside an utterance if it is this share of the utterance's peak (room chatter 12 dB down does not hold it open)
    abs_start: float = 0.012      # never start below this RMS, whatever the floor
    abs_end: float = 0.008
    start_blocks: int = 2         # consecutive blocks above the start threshold
    warmup_s: float = 1.0         # after enabling: listen to the room first, set the noise floor from it
    preroll_s: float = 0.35
    tail_s: float = 0.40          # trailing silence kept in the clip
    max_s: float = 30.0
    unmute_s: float = 0.35        # stay deaf this long after muted ends (room decay of the tool's own sound)


class Segmenter:
    def __init__(self, params: VadParams | None = None):
        self.p = params or VadParams()
        self.reset()

    # -- state ---------------------------------------------------------------------------------
    def reset(self):
        self.n = 0                                # blocks seen
        self.floor = 0.004
        self.pre = collections.deque(maxlen=int(self.p.preroll_s * SR / BLOCK) + 1)
        self.cand: list[np.ndarray] = []          # blocks of a possible start (not yet confirmed)
        self.buf: list[np.ndarray] = []
        self.active = False
        self.silent = 0
        self.voiced = 0
        self.peak = 0.0
        self.last_voiced_n = 0
        self.t_start = 0.0
        self.deaf_until = 0
        self._warm_blocks = int(self.p.warmup_s * SR / BLOCK)
        self._warm: list[float] = []
        self.level = 0.0
        self.discarded = 0                        # utterances thrown away (too short, too quiet)
        self.muted_drops = 0

    @property
    def t(self) -> float:
        return self.n * BLOCK / SR

    @property
    def start_thr(self) -> float:
        return max(self.p.abs_start, self.floor * self.p.start_ratio)

    @property
    def end_thr(self) -> float:
        return max(self.p.abs_end, self.floor * self.p.end_ratio)

    # -- main entry ----------------------------------------------------------------------------
    def feed(self, blk: np.ndarray, muted: bool = False) -> Utterance | None:
        blk = np.asarray(blk, dtype=np.float32)
        self.n += 1
        if muted:
            if self.active or self.cand:
                self.muted_drops += 1
            self._drop()
            self.pre.clear()
            self.deaf_until = self.n + int(self.p.unmute_s * SR / BLOCK)
            self.level = 0.0
            return None
        rms = float(np.sqrt(np.mean(blk * blk)))
        self.level = rms
        if self.n < self.deaf_until:
            self.pre.clear()
            return None
        p = self.p
        if self.n <= self._warm_blocks:                # warm-up: the room alone sets the floor
            self._warm.append(rms)
            self.floor = max(float(np.median(self._warm)), 1e-4)
            self.pre.append(blk)
            return None
        if not self.active:
            if rms > self.start_thr and self.n > self._warm_blocks:
                self.cand.append(blk)
                if len(self.cand) >= p.start_blocks:
                    self.active = True
                    self.buf = list(self.pre) + self.cand
                    self.t_start = (self.n - len(self.buf)) * BLOCK / SR
                    self.pre.clear()
                    self.cand = []
                    self.silent = 0
                    self.voiced = p.start_blocks
                    self.peak = max(float(np.sqrt(np.mean(b * b))) for b in self.buf[-p.start_blocks:])
                    self.last_voiced_n = self.n
                return None
            if self.cand:                          # candidate broke off: it was a click, keep it as noise history
                self.pre.extend(self.cand)
                self.cand = []
            self.pre.append(blk)
            # noise floor: follows quiet closely, rises slowly (a fan spinning up), never from speech
            if rms < self.floor * 2.0 or rms < p.abs_start:
                self.floor += 0.05 * (rms - self.floor)
            else:
                self.floor += 0.01 * (rms - self.floor)
            self.floor = max(self.floor, 1e-4)
            return None
        # inside an utterance
        self.buf.append(blk)
        if rms > max(self.end_thr, p.rel_end * self.peak):
            self.silent = 0
            self.voiced += 1
            self.last_voiced_n = self.n
            self.peak = max(self.peak, rms)
        else:
            self.silent += 1
        too_long = len(self.buf) * BLOCK / SR >= p.max_s
        if too_long:       # a 30 s "utterance" is a room that got louder (a fan switched on): learn it as the new floor
            self.floor = max(float(np.median([np.sqrt(np.mean(b * b)) for b in self.buf[-int(SR / BLOCK):]])), self.floor)
        if self.silent * BLOCK / SR >= p.gap_s or too_long:
            return self._finish()
        return None

    def _drop(self):
        self.active = False
        self.cand = []
        self.buf = []
        self.silent = self.voiced = 0
        self.peak = 0.0

    def _finish(self) -> Utterance | None:
        p = self.p
        keep_tail = int(p.tail_s * SR / BLOCK)
        end_i = len(self.buf) - max(self.silent - keep_tail, 0)
        audio = np.concatenate(self.buf[:end_i]) if end_i > 0 else np.zeros(0, np.float32)
        u = Utterance(audio=audio, t_start=self.t_start, t_speech_end=self.last_voiced_n * BLOCK / SR, t_emit=self.t,
                      voiced_s=self.voiced * BLOCK / SR, peak=self.peak, floor=self.floor)
        self._drop()
        if u.voiced_s < p.min_voiced_s or u.peak < p.min_peak:
            self.discarded += 1
            return None
        return u

    @property
    def in_speech(self) -> bool:
        return self.active


# --- stream helpers used by the simulator backend and the measurements --------------------------
def noise(kind: str, n: int, level_rms: float, rng: np.random.Generator) -> np.ndarray:
    """Background noise at a given RMS: 'white', 'pink', 'hum' (50 Hz mains + harmonics + a little white), 'fan' (low pink + 120 Hz)."""
    t = np.arange(n) / SR
    if kind == "white":
        x = rng.standard_normal(n)
    elif kind in ("pink", "fan"):
        f = np.fft.rfft(rng.standard_normal(n))
        fr = np.fft.rfftfreq(n, 1 / SR)
        fr[0] = fr[1]
        x = np.fft.irfft(f / np.sqrt(fr), n)
        if kind == "fan":
            x = x + 0.6 * x.std() * np.sin(2 * np.pi * 120 * t)
    elif kind == "hum":
        x = sum(np.sin(2 * np.pi * 50 * k * t + rng.uniform(0, 6.28)) / k for k in (1, 2, 3, 5)) + 0.1 * rng.standard_normal(n)
    else:
        raise ValueError(kind)
    x = x - x.mean()
    return (x / (x.std() + 1e-12) * level_rms).astype(np.float32)


def blocks(audio: np.ndarray):
    for i in range(0, len(audio) - BLOCK + 1, BLOCK):
        yield audio[i:i + BLOCK]
