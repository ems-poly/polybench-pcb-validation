# Ported from the earlier voice prototype prg/engine.py (capture part). Changes: no session/grammar/LLM code (the controller
# owns that); the recorder is opened only while voice is enabled AND the page is visible; STT lives here (Whisper kept hot, loaded
# lazily, offline when cached; "sidecar" backend reads <clip>.txt for CI); results go to on_text/on_drop callbacks; mute while the
# reply is spoken comes from tts.speaking(); no partial transcripts, no cue sound, no simulator stream (feed_wav replaces it).
"""Capture: push-to-talk and hands-free VAD, gates, speech to text. No engine knowledge."""
from __future__ import annotations

import collections
import importlib.util
import os
import queue
import tempfile
import threading
import re
import time
from pathlib import Path
from typing import Callable

from polybench.extras import lazy as _lazy
np = _lazy("numpy", "voice")

from . import stt, vad

INSTALL = "pip install 'polybench-pcb-validation[voice]'"
NO_SPEECH_PEAK = 0.02          # block RMS; below this nothing was said (mic muted, wrong input, key pressed in silence)
HALLUCINATIONS = {"", "you", "thank you", "thanks", "thank you.", "thanks for watching", "bye", "the end", "so", "okay", "uh", "um", "hmm"}
OWN_READOUT = "dropped: own readout"      # UIR-37: hands-free input heard while the app was speaking (or in the tail after)
QUIET_FILLERS = HALLUCINATIONS | {"what", "oh", "ah", "huh", "yeah", "mm", "mhm"}


def is_noise(text: str, audio_peak: float | None = None, audio_s: float | None = None) -> bool:
    """A transcript that is only noise: one short token repeated 3 times or more ("What? What? What?"), or a known Whisper
    filler ("Thank you.", "What?", "you") from a quiet or short clip."""
    words = re.findall(r"[a-z']+", (text or "").lower())
    if len(words) >= 3 and len(set(words)) == 1 and len(words[0]) <= 8:
        return True
    norm = (text or "").strip().lower().strip(" .!?,")
    quiet = audio_peak is not None and (audio_peak < 0.2 or (audio_s is not None and audio_s < 1.2))
    return quiet and (norm in QUIET_FILLERS or norm.startswith("thanks for watching"))


HF_CACHE = Path("~/.cache/huggingface/hub").expanduser()


def level_stats(audio: np.ndarray) -> dict:
    """Peak 50 ms RMS and the quietest 10 %: how loud the speech was and how noisy the room is (full scale 0..1)."""
    n = len(audio) // 800
    if n < 1:
        return {"peak_rms": 0.0, "floor_rms": 0.0}
    r = np.sqrt((audio[:n * 800].reshape(n, 800) ** 2).mean(axis=1))
    return {"peak_rms": round(float(r.max()), 4), "floor_rms": round(float(np.percentile(r, 10)), 4)}


def model_cached(model: str) -> bool:
    return (HF_CACHE / ("models--" + model.replace("/", "--"))).exists() or Path(model).expanduser().exists()


def cached_models() -> list[str]:
    """Whisper models in the local Hugging Face cache (the only ones the STT may load)."""
    try:
        return sorted(d.name[len("models--"):].replace("--", "/") for d in HF_CACHE.iterdir()
                      if d.name.startswith("models--") and "whisper" in d.name.lower())
    except OSError:
        return []


class Capture:
    """on_text(text, source, info) -> entry and on_drop(reason, text, source, info) -> entry are called from worker threads
    (or from the caller's thread in feed_wav). info = {audio, stt_ms, ...}."""

    def __init__(self, get_cfg: Callable[[], dict], speaking: Callable[[], bool], on_text: Callable, on_drop: Callable,
                 on_change: Callable[[], None] | None = None, prompt: Callable[[], str] | None = None,
                 cache_dir: str | None = None, recorder_factory: Callable | None = None, tail_s: float = 0.25,
                 clock: Callable[[], float] = time.monotonic):
        self.cfg = get_cfg
        self.speaking = speaking
        self.on_text, self.on_drop = on_text, on_drop
        self.on_change = on_change or (lambda: None)
        self.prompt = prompt
        self.cache = cache_dir or os.path.join(tempfile.gettempdir(), "polybench-voice", time.strftime("%Y%m%d-%H%M%S"))
        self.recorder_factory = recorder_factory
        self.tail_s = tail_s
        self.clock = clock                    # the mute window runs on this clock (tests pass a fake one)
        self.enabled = False
        self.handsfree = False
        self._vis: dict[str, tuple[bool, float]] = {}      # client id -> (visible, last post); the mic opens if any client is visible
        self._now = time.monotonic
        self.recording = False
        self.busy = False
        self.mic_error = ""
        self.rec = None
        self.restarts_closed = 0              # restarts of recorders already closed (the counter survives voice off/on)
        self._sync_lock = threading.RLock()
        self.whisper = None
        self.whisper_error = ""
        self.loading = False
        self.work = threading.Lock()          # one clip through STT and the pipeline at a time
        self.stt_lock = threading.Lock()
        self._fin = threading.Lock()          # held between key release and the end of the post-roll
        self._gen = 0
        self._beat = 0.0
        self._mute_until = 0.0
        self._speak_seen = 0.0
        self._t_press = 0.0
        self.clip_n = 0
        # the tail after a readout is Capture's (cfg own_readout_tail_s, muted()); the segmenter adds none of its own, so the tail is that long and no longer
        self.seg = vad.Segmenter(vad.VadParams(gap_s=self.cfg()["hf_gap_s"], unmute_s=0.0))
        self._own = {"blocks": 0, "peak": 0.0}       # voiced blocks heard while muted (the app's own readout, UIR-37)
        self.hf = {"captured": 0, "dropped": 0}
        self.hfq: queue.Queue = queue.Queue()
        self.audit: collections.deque = collections.deque(maxlen=200)
        threading.Thread(target=self._hf_worker, daemon=True, name="hf-worker").start()
        threading.Thread(target=self._watchdog, daemon=True, name="ptt-watchdog").start()

    # ---- microphone: open only while enabled and visible -----------------------------------------
    @staticmethod
    def mic_status() -> dict:
        if importlib.util.find_spec("sounddevice") is None:
            return {"ok": False, "detail": "sounddevice is not installed", "install": INSTALL}
        return {"ok": True, "detail": "", "install": ""}

    def _sync(self):
        with self._sync_lock:                         # every path inside is bounded (open waits <1 s, close hands over to a reaper)
            self._sync_locked()

    def _sync_locked(self):
        want = self.enabled and self.visible
        if want and self.rec is None:
            try:
                if self.recorder_factory:
                    self.rec = self.recorder_factory(self.cfg()["input_device"])
                else:
                    from .audio import Recorder
                    self.rec = Recorder(self.cfg()["input_device"], on_event=self._log)
                self.mic_error = ""
            except Exception as e:                    # missing module, no device, permission
                self.rec = None
                self.mic_error = f"microphone not available: {e}"
        elif not want and self.rec is not None:
            self.recording = False
            self._gen += 1
            old, self.rec = self.rec, None
            self.restarts_closed += getattr(old, "restarts", 0)
            try:
                old.close()                           # returns within a fraction of a second even when CoreAudio is wedged
            except Exception:
                pass
            self.seg.reset()
        if self.rec is not None:
            self.rec.tap = self.hf_block if self.handsfree else None
        self.on_change()

    def set_enabled(self, on: bool):
        self.enabled = bool(on)
        self._sync()
        if self.enabled:
            self.preload()

    def set_handsfree(self, on: bool):
        self.handsfree = bool(on)
        self.seg.p.gap_s = self.cfg()["hf_gap_s"]
        self.seg.reset()
        self._sync()

    VIS_TTL_S = 45.0       # a visible client that stops posting (tab closed, laptop asleep) is forgotten; pages re-post every 15 s

    @property
    def visible(self) -> bool:
        """True if any client (browser tab) reports itself visible. A hidden tab or a closed headless browser never mutes a visible one."""
        now = self._now()
        return any(v and now - t < self.VIS_TTL_S for v, t in list(self._vis.values()))

    def set_visible(self, on: bool, client: str | None = None):
        now = self._now()
        self._vis = {k: x for k, x in self._vis.items() if now - x[1] < self.VIS_TTL_S}
        self._vis[client or "_"] = (bool(on), now)
        self._sync()

    def apply_config(self):
        self.seg.p.gap_s = self.cfg()["hf_gap_s"]
        if self.whisper is not None and self.whisper.model != self.cfg()["stt_model"]:
            self.whisper = None

    # ---- STT -----------------------------------------------------------------------------------------
    def stt_status(self) -> dict:
        c = self.cfg()
        if c["stt_backend"] == "sidecar":
            return {"ok": True, "model": "sidecar", "detail": "reads <clip>.txt next to each WAV", "install": ""}
        m = c["stt_model"]
        if importlib.util.find_spec("mlx_whisper") is None:
            return {"ok": False, "model": m, "detail": "mlx-whisper is not installed", "install": INSTALL}
        if not model_cached(m):
            return {"ok": False, "model": m, "detail": "STT model not downloaded: run once with network",
                    "install": f"HF_HUB_OFFLINE=0 .venv/bin/python -c \"import mlx_whisper, numpy; mlx_whisper.transcribe(numpy.zeros(16000, 'float32'), path_or_hf_repo='{m}')\""}
        if self.whisper_error:
            return {"ok": False, "model": m, "detail": self.whisper_error, "install": ""}
        return {"ok": True, "model": m, "cached": cached_models(), "detail": "ready" if self.whisper and self.whisper.warm else ("loading" if self.loading else "loads on first use"),
                "install": ""}

    def _get_whisper(self):
        c = self.cfg()
        if self.whisper is None or self.whisper.model != c["stt_model"]:
            st = self.stt_status()
            if not st["ok"]:
                raise RuntimeError(st["detail"])
            os.environ["HF_HUB_OFFLINE"] = "1"         # no cloud calls: the cached model only (stt_status refused an uncached one above)
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            self.whisper = stt.Whisper(model=c["stt_model"])
        if self.prompt:
            try:
                self.whisper.prompt = self.prompt()
            except Exception:
                pass
        return self.whisper

    def preload(self):
        """Load and warm Whisper in a thread so the first utterance is fast."""
        if self.cfg()["stt_backend"] != "whisper" or self.loading or (self.whisper and self.whisper.warm):
            return
        if not self.stt_status()["ok"]:
            return

        def run():
            self.loading = True
            try:
                with self.stt_lock:
                    self._get_whisper().warmup()
                self.whisper_error = ""
            except Exception as e:
                self.whisper_error = f"STT failed to load: {e}"
            finally:
                self.loading = False
                self.on_change()
        threading.Thread(target=run, daemon=True, name="stt-load").start()

    def transcribe(self, audio: np.ndarray, path: str | None) -> str:
        if self.cfg()["stt_backend"] == "sidecar":
            if not path:
                raise RuntimeError("sidecar STT needs a clip path")
            for side in (Path(path).with_suffix(".txt"), Path(str(path) + ".txt")):
                if side.exists():
                    return side.read_text(encoding="utf-8").strip()
            raise RuntimeError(f"no sidecar transcript next to {path}")
        with self.stt_lock:
            return self._get_whisper().transcribe(audio)

    # ---- push to talk ---------------------------------------------------------------------------------
    def ptt_start(self, beats: bool = False) -> bool:
        if self.recording or self.rec is None:
            return False
        with self._fin:                              # a previous release may still be finishing its post-roll
            pass
        self.recording = True
        self._beat = time.monotonic() if beats else 0.0
        self.seg.reset()
        self._t_press = time.perf_counter()
        self._gen += 1
        self.rec.start()
        self.on_change()
        return True

    def ptt_beat(self):
        self._beat = time.monotonic()

    def ptt_cancel(self):
        if self.rec is not None and self.recording:
            self.rec.cancel()
        self.recording = False
        self._gen += 1
        self.on_change()

    def ptt_stop(self) -> bool:
        if not self.recording:
            return False
        self.recording = False
        self._gen += 1
        self._fin.acquire()
        self.busy = True
        self.on_change()
        threading.Thread(target=self._finish_ptt, args=(time.perf_counter() - self._t_press,), daemon=True, name="ptt-finish").start()
        return True

    def _finish_ptt(self, held_s: float):
        """Post-roll after the release (the last word is often still leaving the mouth), then the same path as any clip."""
        try:
            time.sleep(self.tail_s)
            audio = self.rec.stop() if self.rec is not None else np.zeros(0, np.float32)
        finally:
            self._fin.release()
        info = {"event": "ptt", "held_s": round(held_s, 2), "audio_s": round(len(audio) / stt.SR, 2), **level_stats(audio)}
        if len(audio) < 0.25 * stt.SR:
            self._log({**info, "result": "dropped", "reason": "too short"})
            self._drop(f"too short ({held_s:.2f} s held)", None, None, "ptt", info)
            self.busy = False
            self.on_change()
            return
        try:
            path = self._save(audio)
            self._log({**info, "audio": path})
            self._process(audio, path, "ptt")
        finally:
            self.busy = False
            self.on_change()

    def _check_stream(self):
        """Audio watchdog: a stream that should be open but delivers no blocks is replaced (see audio.Recorder.check)."""
        rec = self.rec
        if rec is None or not hasattr(rec, "check"):
            return
        try:
            rec.check()
            err = getattr(rec, "error", "")
            self.mic_error = f"microphone not available: {err}" if err and getattr(rec, "stream", 1) is None else ""
        except Exception as e:
            self._log({"event": "error", "reason": f"audio watchdog failed: {e}"})

    def stream_info(self) -> dict:
        """For /api/voice/status: seconds since the last audio block (None when no stream is wanted), restarts, and the stalling flag."""
        rec = self.rec
        if rec is None:
            return {"stream_age_s": None, "restarts": self.restarts_closed, "stalling": False}
        return {"stream_age_s": round(float(getattr(rec, "stream_age_s", 0.0)), 2),
                "restarts": self.restarts_closed + int(getattr(rec, "restarts", 0)), "stalling": bool(getattr(rec, "stalling", False))}

    def _watchdog(self):
        """A lost key-up must not leave the recorder open; a stalled microphone stream is replaced."""
        while True:
            time.sleep(0.5)
            self._check_stream()
            if self.recording and self._beat and time.monotonic() - self._beat > 2.0:
                self._log({"event": "ptt", "result": "auto_stop", "reason": "no key heartbeat for 2 s (key-up lost: focus left the page?)"})
                self.ptt_stop()

    # ---- hands-free ------------------------------------------------------------------------------------
    def tail_s_own(self) -> float:
        return max(0.0, float(self.cfg().get("own_readout_tail_s", 0.5)))

    def muted(self) -> bool:
        """True while the tool itself is speaking and for own_readout_tail_s after (UIR-37, half-duplex): the microphone must not hear that."""
        now = self.clock()
        if self.speaking():
            self._speak_seen = now
            return True
        return now < self._mute_until or now - self._speak_seen < self.tail_s_own()

    def mute_for(self, seconds: float):
        self._mute_until = max(self._mute_until, self.clock() + seconds)

    def hf_block(self, blk, muted: bool | None = None):
        """One 32 ms block (audio thread in live mode)."""
        if not self.handsfree or self.recording:
            if self.seg.active:
                self.seg.reset()
            self._own["blocks"], self._own["peak"] = 0, 0.0
            return None
        m = self.muted() if muted is None else muted
        self._hear_own(blk, m)
        u = self.seg.feed(blk, muted=m)
        if u is not None:
            self.hf["captured"] += 1
            self.hfq.put(u)
            self.on_change()
        return u

    def _hear_own(self, blk, muted: bool):
        """Voiced sound while muted is the app's own readout reaching the microphone (or an utterance that overlapped it): count it, and when
        the mute ends queue one 'dropped: own readout' for the journal. Never transcribed, never acted on."""
        o = self._own
        if muted:
            rms = float(np.sqrt(np.mean(np.asarray(blk, dtype=np.float32) ** 2)))
            if rms >= self.seg.p.min_peak:
                o["blocks"] += 1
                o["peak"] = max(o["peak"], rms)
            return
        if o["blocks"]:
            n, peak = o["blocks"], o["peak"]
            o["blocks"], o["peak"] = 0, 0.0
            if n * vad.BLOCK / vad.SR >= self.seg.p.min_voiced_s:      # a click is not an utterance
                self.hfq.put({"own": True, "voiced_s": round(n * vad.BLOCK / vad.SR, 2), "peak": round(peak, 3)})

    def _hf_worker(self):
        while True:
            u = self.hfq.get()
            if isinstance(u, dict):
                try:
                    self._drop(OWN_READOUT, None, None, "handsfree", {"voiced_s": u["voiced_s"], "peak": u["peak"]})
                except Exception as e:
                    self._log({"event": "error", "reason": f"own-readout drop failed: {e}"})
                continue
            try:
                self.busy = True
                self.on_change()
                path = self._save(u.audio)
                self._log({"event": "handsfree", "audio": path, "audio_s": round(len(u.audio) / stt.SR, 2), "voiced_s": round(u.voiced_s, 2),
                           "peak": round(u.peak, 3), "floor": round(u.floor, 4), "gap_s": self.seg.p.gap_s})
                self._process(u.audio, path, "handsfree")
            except Exception as e:
                self._log({"event": "error", "reason": f"hands-free processing failed: {e}"})
            finally:
                self.busy = False
                self.on_change()

    # ---- the common path: gates -> STT -> callbacks -------------------------------------------------
    def _save(self, audio: np.ndarray) -> str:
        os.makedirs(self.cache, exist_ok=True)
        self.clip_n += 1
        path = os.path.join(self.cache, f"{self.clip_n:03d}.wav")
        stt.write_wav(path, audio)
        return path

    def _log(self, d: dict):
        self.audit.append({"t": time.strftime("%H:%M:%S"), **d})

    def _drop(self, why: str, path, text, source, info=None) -> dict:
        self.hf["dropped"] += 1
        self._log({"event": "dropped", "source": source, "reason": why, "transcript": text, "audio": path})
        return self.on_drop(why, text, source, {**(info or {}), "audio": path})

    def _process(self, audio: np.ndarray, path: str | None, source: str, stt_path: str | None = None) -> dict:
        """Gates, STT, callback. `stt_path` is where a sidecar transcript is looked up (the original WAV for feed_wav)."""
        with self.work:
            try:
                st = level_stats(audio)
                if st["peak_rms"] < NO_SPEECH_PEAK:
                    return self._drop(f"no speech: loudest 50 ms was {st['peak_rms']:.3f}, mic level too low or wrong input", path, None, source, st)
                t = time.perf_counter()
                try:
                    text = self.transcribe(audio, stt_path or path)
                except Exception as e:
                    return self._drop(f"speech to text failed: {e}", path, None, source, st)
                ms = round(1000 * (time.perf_counter() - t))
                if (is_noise(text, st["peak_rms"], len(audio) / stt.SR) if self.cfg()["stt_backend"] == "whisper" else is_noise(text)):
                    return self._drop("noise: Whisper heard nothing real", path, text, source, {**st, "stt_ms": ms})
                return self.on_text(text, source, {"audio": path, "stt_ms": ms, **st})
            finally:
                self.on_change()

    def feed_wav(self, path: str, via: str = "ptt") -> dict:
        """Test and demo path: a WAV through the same gates, STT and pipeline, on the caller's thread.
        via='vad' runs it through a private voice-activity segmenter (room noise before and silence after) first."""
        audio = stt.read_wav(path)
        if via == "vad":
            rng = np.random.default_rng(1)
            sig = np.concatenate([vad.noise("pink", int(1.5 * stt.SR), 0.004, rng), audio, np.zeros(int(2.5 * stt.SR), np.float32)]).astype(np.float32)
            sig = sig + vad.noise("pink", len(sig), 0.004, rng)
            seg = vad.Segmenter(vad.VadParams(gap_s=self.cfg()["hf_gap_s"]))
            u = None
            for blk in vad.blocks(sig):
                u = seg.feed(blk) or u
                if u is not None:
                    break
            if u is None:
                return self._drop("no utterance found by the voice-activity detector", path, None, "clip")
            self.hf["captured"] += 1
            return self._process(u.audio, path, "clip", stt_path=path)
        return self._process(audio, path, "clip", stt_path=path)

    # ---- status ----------------------------------------------------------------------------------------
    def level(self) -> float:
        return round(self.rec.level if self.rec is not None else 0.0, 4)

    def close(self):
        self.enabled = False
        self._sync()
