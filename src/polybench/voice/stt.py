# Ported from the earlier voice prototype prg/stt.py (read-only PoC). Changes for Polybench: build_prompt only (Polybench test IDs, boards S01..Sxx, command vocabulary).
"""Speech to text with local Whisper (mlx-whisper on Apple silicon). No network at run time."""
from __future__ import annotations

import time
import wave

from polybench.extras import lazy as _lazy
np = _lazy("numpy", "voice")

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
SR = 16000


def read_wav(path: str) -> np.ndarray:
    """16 kHz mono float32 from a WAV file. Other rates are resampled linearly (adequate for speech)."""
    with wave.open(path, "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise ValueError("only 16-bit PCM WAV is supported")
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if sr != SR:
        n = int(len(a) * SR / sr)
        a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)
    return a


def write_wav(path: str, audio: np.ndarray):
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def build_prompt(ids: list[str], boards: list[str]) -> str:
    """Whisper initial prompt: biases decoding toward the sheet's vocabulary (max ~224 tokens)."""
    sample = []
    seen = set()
    for i in ids:
        k = i[:2].lower()
        if k not in seen or len(sample) < 6:
            sample.append(i)
            seen.add(k)
        if len(sample) >= 14:
            break
    b = ", ".join(boards) if len(boards) <= 4 else f"{boards[0]} to {boards[-1]}"
    ex = sample[0] if sample else "U01"
    first = boards[0] if boards else "S01"
    return (f"Bench measurement dictation. Boards {b}. Readings such as: {first} 0.2 volts, board three, all boards 2 ohms, "
            f"{ex} 3.31 volts, 4.7 kilohms, 22 milliamps, 2.5 millivolts per volt. IDs: {', '.join(sample)}. "
            "Accept. Accept fail. Reject. Cancel. Undo. Skip. Next test. Previous. Go to test. Note. Repeat. Which test. Progress. Show it in KiCad.")


class Whisper:
    def __init__(self, model: str = DEFAULT_MODEL, prompt: str | None = None):
        self.model = model
        self.prompt = prompt
        self.warm = False

    def transcribe(self, audio: np.ndarray) -> str:
        import mlx_whisper
        r = mlx_whisper.transcribe(audio, path_or_hf_repo=self.model, language="en", temperature=0.0,
                                   condition_on_previous_text=False, initial_prompt=self.prompt,
                                   no_speech_threshold=0.6, verbose=None)
        return r["text"].strip()

    def warmup(self):
        t = time.perf_counter()
        self.transcribe(np.zeros(SR, dtype=np.float32))
        self.warm = True
        return time.perf_counter() - t
