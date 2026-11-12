# Ported from the voice proof of concept as a TEST/EVAL HARNESS only (the PoC pipeline around the ported modules). Changes: imports. Not used by the live app.
"""Pipeline: audio -> Whisper -> grammar -> (LLM fallback) -> validation -> session."""
from __future__ import annotations

import collections
import datetime as dt
import glob
import os
import queue
import re
import subprocess
import threading
import time

import numpy as np

from polybench.voice import extract, grammar, llm, rowmode, stt, validate, vad
from polybench.voice.session import Session, now_iso


class Engine:
    def __init__(self, session: Session, backend: str = "live", whisper_model: str = stt.DEFAULT_MODEL,
                 use_llm: bool = True, llm_model: str = llm.DEFAULT_MODEL, clips: list[str] | None = None,
                 speak: bool = False, cache_dir: str | None = None, device=None, no_stt: bool = False,
                 live_partials: bool = True, partial_interval: float = 0.7, handsfree: bool = False, hf_gap_s: float = 0.8,
                 cue_sound: bool = True, tail_s: float = 0.25):
        self.s = session
        self.backend = backend
        self.use_llm = use_llm and llm.available()
        self.llm_model = llm_model
        self.speak = speak
        self.clips = list(clips or [])
        self.clip_i = 0
        self.last: dict = {}
        self.busy = False
        self.recording = False
        self.rec = None
        self.whisper = None
        self.work = threading.Lock()
        self.stt_lock = threading.Lock()          # one Whisper call at a time; a partial in flight delays the final by its remainder
        self.live_partials = live_partials
        self.partial_interval = partial_interval
        self.partial = {"seq": 0, "text": "", "audio_s": 0.0, "ms": None}
        self._gen = 0
        self.traces = collections.deque(maxlen=10)
        self._trace_n = 0
        base = cache_dir or os.path.expanduser("~/.cache/polybench-voice-eval")
        self.cache = os.path.join(base, dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
        self.clip_n = 0
        self._say = None
        self._mute_until = 0.0                  # monotonic time before which the mic is deaf (cue sound, read-back tail)
        self._say_seen = 0.0
        self.cue_sound = cue_sound
        self.tail_s = tail_s
        self._fin = threading.Lock()            # held between key release and the end of the post-roll
        self._beat = 0.0                        # last PTT heartbeat from the page (0: none ever seen)
        self.seg = vad.Segmenter(vad.VadParams(gap_s=hf_gap_s))
        self.hf = {"on": False, "captured": 0, "dropped": 0, "last_ms": None}
        self.hfq: queue.Queue = queue.Queue()
        self.sim_stream_thread = None
        threading.Thread(target=self._hf_worker, daemon=True, name="hf-worker").start()
        threading.Thread(target=self._watchdog, daemon=True, name="ptt-watchdog").start()
        if not no_stt:
            self.whisper = stt.Whisper(prompt=stt.build_prompt([r["id"] for r in session.script.rows], session.script.boards),
                                       model=whisper_model)
        if backend == "live":
            from polybench.voice.audio import Recorder
            self.rec = Recorder(device)
        if handsfree:
            self.set_handsfree(True)

    # ---- startup -----------------------------------------------------------------------------
    def warmup(self):
        out = {}
        if self.whisper:
            out["whisper_warmup_s"] = round(self.whisper.warmup(), 2)
        if self.use_llm:
            t = time.perf_counter()
            try:
                llm.extract("next", self.s.script, extract.context_state(self.s.script, self.s.ctx, self.s.effective, []), self.llm_model)
                out["llm_warmup_s"] = round(time.perf_counter() - t, 2)
            except Exception as e:
                self.use_llm = False
                out["llm_error"] = str(e)
        return out

    # ---- push to talk -----------------------------------------------------------------------
    def ptt_start(self, beats: bool = False):
        if self.recording:
            return
        with self._fin:                         # a previous release may still be finishing its post-roll
            pass
        if self._say and self._say.poll() is None:
            self._say.terminate()               # the operator speaks over the read-back
        self.recording = True
        self._beat = time.monotonic() if beats else 0.0
        self.seg.reset()
        self._t_press = time.perf_counter()
        self._gen += 1
        self.partial = {"seq": 0, "text": "", "audio_s": 0.0, "ms": None}
        if self.rec:
            self.rec.start()
            if self.whisper and self.live_partials:
                threading.Thread(target=self._partial_loop, args=(self._gen,), daemon=True, name="partials").start()
        self.s._bump()

    def _partial_loop(self, gen: int, get_audio=None):
        """While the key is held, re-transcribe the growing buffer. Whisper pads every window to 30 s, so a call costs about
        the same for 1 s or 5 s of speech; the interval sets the duty cycle. The loop yields to the final transcription."""
        get_audio = get_audio or self.rec.snapshot
        done_len = 0
        while self.recording and self._gen == gen:
            time.sleep(0.05)
            a = get_audio()
            if len(a) < 0.5 * stt.SR or len(a) - done_len < 0.3 * stt.SR:
                continue
            t = time.perf_counter()
            with self.stt_lock:
                if not self.recording or self._gen != gen:
                    return                      # released while waiting: do not delay the final
                try:
                    text = self.whisper.transcribe(a)
                except Exception:
                    return
            ms = 1000 * (time.perf_counter() - t)
            done_len = len(a)
            if self.recording and self._gen == gen:
                self.partial = {"seq": self.partial["seq"] + 1, "text": text, "audio_s": round(len(a) / stt.SR, 2), "ms": round(ms)}
            slack = self.partial_interval - (time.perf_counter() - t)
            if slack > 0:
                time.sleep(slack)

    def ptt_cancel(self):
        if self.rec and self.recording:
            self.rec.cancel()
        self.recording = False
        self.s._bump()

    def ptt_stop(self, t_release: float | None = None) -> bool:
        """Release of the key. Starts processing in a worker thread. Returns False if nothing was recording."""
        if not self.recording:
            return False
        t_release = t_release or time.perf_counter()
        self.recording = False
        self._gen += 1
        audio = None
        path = None
        if self.backend == "simulator":
            if self.clip_i >= len(self.clips):
                self.s.say("Simulator: no more clips", "error")
                return False
            path = self.clips[self.clip_i]
            self.clip_i += 1
            audio = stt.read_wav(path)
        else:
            self._fin.acquire()
            self.busy = True
            self.s._bump()
            threading.Thread(target=self._finish_ptt, args=(t_release, time.perf_counter() - getattr(self, "_t_press", t_release)), daemon=True).start()
            return True
        self.busy = True
        self.s._bump()
        threading.Thread(target=self._process, args=(audio, path, t_release), daemon=True).start()
        return True

    def _finish_ptt(self, t_release: float, held_s: float):
        """Post-roll after the key is released (the last word is often still leaving the mouth), then the same path as any clip."""
        try:
            time.sleep(self.tail_s)
            audio = self.rec.stop()
        finally:
            self._fin.release()
        info = {"event": "ptt", "held_s": round(held_s, 2), "audio_s": round(len(audio) / stt.SR, 2), **self.level_stats(audio)}
        if len(audio) < 0.25 * stt.SR:
            self.s._log({**info, "result": "dropped", "reason": "too short"})
            self.s.say(f"Too short ({held_s:.2f} s held), nothing recorded", "error")
            self.busy = False
            self.s._bump()
            return
        os.makedirs(self.cache, exist_ok=True)
        self.clip_n += 1
        path = os.path.join(self.cache, f"{self.clip_n:03d}.wav")
        stt.write_wav(path, audio)
        self.s._log({**info, "audio": path})
        self._process(audio, path, t_release)

    @staticmethod
    def level_stats(audio: np.ndarray) -> dict:
        """Peak 50 ms RMS and the quietest 10 %: how loud the speech was and how noisy the room is (both in full scale 0..1)."""
        n = len(audio) // 800
        if n < 1:
            return {"peak_rms": 0.0, "floor_rms": 0.0}
        r = np.sqrt((audio[:n * 800].reshape(n, 800) ** 2).mean(axis=1))
        return {"peak_rms": round(float(r.max()), 4), "floor_rms": round(float(np.percentile(r, 10)), 4)}

    # ---- watchdog: a lost key-up must not leave the recorder open -------------------------------
    def ptt_beat(self):
        self._beat = time.monotonic()

    def _watchdog(self):
        while True:
            time.sleep(0.5)
            if self.recording and self._beat and time.monotonic() - self._beat > 2.0:
                self.s._log({"event": "ptt", "result": "auto_stop", "reason": "no key heartbeat for 2 s (key-up lost: focus left the page?)"})
                self.s.say("Key release was lost (the page lost focus); processing what was recorded", "error")
                self.ptt_stop()

    NO_SPEECH_PEAK = 0.02          # block RMS; below this nothing was said (mic muted, wrong input, key pressed in silence)
    HALLUCINATIONS = {"", "you", "thank you", "thanks", "thank you.", "thanks for watching", "bye", "the end", "so", "okay", "uh", "um", "hmm"}

    def _drop(self, why: str, path, text: str | None, stats: dict, source: str = "voice"):
        self.s._log({"event": "dropped", "source": source, "reason": why, "transcript": text, "audio": path, **stats})
        self.s.say(f"Nothing used: {why}" + (f" (heard \"{text}\")" if text else ""), "error")
        self.hf["dropped"] += 1

    def _process(self, audio, path, t0, source="voice"):
        with self.work:
            try:
                st = self.level_stats(audio)
                if self.whisper and st["peak_rms"] < self.NO_SPEECH_PEAK:
                    self._drop(f"no speech: loudest 50 ms was {st['peak_rms']:.3f}, mic level too low or wrong input", path, None, st, source)
                    return
                t1 = time.perf_counter()
                with self.stt_lock:
                    t1b = time.perf_counter()
                    text = self.whisper.transcribe(audio) if self.whisper else ""
                t2 = time.perf_counter()
                norm = text.strip().lower().strip(" .!?,")
                if self.whisper and (norm in self.HALLUCINATIONS or norm.startswith("thanks for watching")) and (st["peak_rms"] < 0.2 or len(audio) < 1.2 * stt.SR):
                    self._drop("Whisper heard nothing real", path, text, st, source)
                    return
                self.handle_text(text, source=source, audio=path, t0=t0, stt_s=t2 - t1b,
                                 stt_wait_s=t1b - t1, partial_text=self.partial.get("text"))
            finally:
                self.busy = False
                self.s._bump()

    # ---- hands-free: voice activity detection ---------------------------------------------------
    def muted(self) -> bool:
        """True while the tool itself is making sound (read-back, cue) and shortly after; the VAD must not hear that."""
        now = time.monotonic()
        if self._say and self._say.poll() is None:
            self._say_seen = now
            return True
        return now < self._mute_until or now - self._say_seen < self.seg.p.unmute_s

    def mute_for(self, seconds: float):
        self._mute_until = max(self._mute_until, time.monotonic() + seconds)

    def set_handsfree(self, on: bool, gap_s: float | None = None) -> dict:
        if gap_s is not None:
            self.seg.p.gap_s = max(0.3, min(3.0, float(gap_s)))
        self.hf["on"] = bool(on)
        self.seg.reset()
        if self.rec is not None:
            self.rec.tap = self.hf_block if on else None
        elif on and self.backend == "simulator" and not (self.sim_stream_thread and self.sim_stream_thread.is_alive()):
            self.start_sim_stream(**getattr(self, "sim_params", {}))
        self.s.say("Hands-free on: speak, a pause of %.1f s ends the utterance" % self.seg.p.gap_s if on else "Hands-free off", "ok")
        self.s._bump()
        return self.hf_status()

    def hf_block(self, blk, muted: bool | None = None):
        """One 32 ms block of microphone audio (audio thread in live mode, the stream feeder in the simulator)."""
        if not self.hf["on"] or self.recording:
            if self.seg.active:
                self.seg.reset()
            return None
        u = self.seg.feed(blk, muted=self.muted() if muted is None else muted)
        if u is not None:
            self.hf["captured"] += 1
            if self.cue_sound and self.backend == "live":
                self._cue()
            self.hfq.put((u, time.perf_counter()))
            self.s._bump()
        return u

    def _cue(self):
        self.mute_for(0.6)
        try:
            subprocess.Popen(["afplay", "/System/Library/Sounds/Tink.aiff"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def _hf_worker(self):
        while True:
            u, t_emit = self.hfq.get()
            try:
                self.busy = True
                self.s._bump()
                os.makedirs(self.cache, exist_ok=True)
                self.clip_n += 1
                path = os.path.join(self.cache, f"{self.clip_n:03d}.wav")
                stt.write_wav(path, u.audio)
                self.s._log({"event": "handsfree", "audio": path, "audio_s": round(len(u.audio) / stt.SR, 2), "voiced_s": round(u.voiced_s, 2),
                             "peak": round(u.peak, 3), "floor": round(u.floor, 4), "gap_s": self.seg.p.gap_s})
                # latency is measured from the end of speech, not from the end of the gap
                self._process(u.audio, path, t_emit - self.seg.p.gap_s, source="voice")
                self.hf["last_ms"] = (self.last or {}).get("ms", {}).get("release_to_preview")
            except Exception as e:
                self.s.say(f"Hands-free processing failed: {e}", "error")
            finally:
                self.busy = False
                self.s._bump()

    def hf_status(self) -> dict:
        sg = self.seg
        return {**self.hf, "gap_s": sg.p.gap_s, "level": round(self.rec.level if self.rec else sg.level, 4), "floor": round(sg.floor, 4),
                "start_thr": round(sg.start_thr, 4), "in_speech": sg.active, "muted": self.muted(),
                "discarded": sg.discarded, "muted_drops": sg.muted_drops, "queued": self.hfq.qsize()}

    def start_sim_stream(self, noise_kind: str = "fan", noise_rms: float = 0.004, gap_range=(1.4, 2.4), speed: float = 1.0, seed: int = 1):
        """Simulator backend: play the clips as a continuous microphone stream (noise, clip, silence, clip, ...) into the VAD."""
        rng = np.random.default_rng(seed)

        def run():
            def play(a):
                for blk in vad.blocks(a):
                    while self.hf["on"] and self.recording:
                        time.sleep(0.05)
                    self.hf_block(blk)
                    if speed > 0:
                        time.sleep(vad.BLOCK / vad.SR / speed)
            play(vad.noise(noise_kind, int(1.5 * vad.SR), noise_rms, rng))
            while self.clip_i < len(self.clips) and self.hf["on"]:
                a = stt.read_wav(self.clips[self.clip_i])
                self.clip_i += 1
                n = vad.noise(noise_kind, len(a), noise_rms, rng)
                play(a + n)
                play(vad.noise(noise_kind, int(rng.uniform(*gap_range) * vad.SR), noise_rms, rng))
        t = threading.Thread(target=run, daemon=True, name="sim-stream")
        t.start()
        self.sim_stream_thread = t
        return t

    # ---- text path (also used for typed input and the simulator without STT) -----------------
    def handle_text(self, text: str, source: str = "typed", audio: str | None = None, t0: float | None = None,
                    stt_s: float | None = None, stt_wait_s: float | None = None, partial_text: str | None = None) -> dict:
        t0 = t0 or time.perf_counter()
        ta = time.perf_counter()
        parse = grammar.parse(text, self.s.idx)
        parse["route"] = "grammar"
        multi = self._try_multi(text)
        if multi:
            parse = {"ok": True, "intent": "multi", "route": "grammar", "transcript": text, "boards": [x["boards"][0] for x in multi],
                     "id": multi[0].get("id"), "value": None, "reason": None, "pairs": [(x["boards"][0], x["value"]) for x in multi]}
        grammar_parse = dict(parse)
        tb = time.perf_counter()
        llm_s = None
        llm_tr = None
        ext = None
        if multi:
            res = self.s.handle_multi(multi, source, text, {"audio": audio, "route": "grammar"})
            if res["ok"] and res.get("targets"):
                self.s.add_derived(extract.derive_for_values(self.s.script, res["targets"], self.s.effective), text, res)
            if not res["ok"]:
                multi = None            # a pair did not bind: let the LLM have the whole utterance
                parse = dict(grammar.parse(text, self.s.idx), route="grammar")
                grammar_parse = dict(parse)
        if multi:
            pass
        elif parse["ok"] and not (self.use_llm and not self.simple(text, parse)):
            res = self.s.handle_parse(parse, source, text, {"audio": audio, "route": "grammar"})      # fast path
            if res["ok"] and parse["intent"] == "value" and res.get("targets"):
                self.s.add_derived(extract.derive_for_values(self.s.script, res["targets"], self.s.effective), text, res)
        elif self.use_llm and text.strip():
            ctx = self.s.ctx
            state = extract.context_state(self.s.script, ctx, self.s.effective, self.s.recent_entries())
            llm_tr = {"model": self.llm_model, "prompt": llm.prompt_summary(self.s.script, state, self.llm_model),
                      "raw": None, "ms": None, "ok": False, "reason": None, "validation": None}
            try:
                raw = llm.extract(text, self.s.script, state, self.llm_model)
                llm_s = time.perf_counter() - tb
                ext = extract.resolve(raw, text, self.s.script, ctx, self.s.effective, self.s.recent_numbers())
                llm_tr.update(raw=raw, ms=round(1000 * llm_s), ok=bool(ext["ok"]),
                              reason=None if ext["ok"] else "; ".join(r["reason"] for r in ext["rejected"]) or "no result in the transcript",
                              validation={k: ext[k] for k in ("checks", "cells", "rejected", "corrections", "unplaced_notes")})
            except Exception as e:
                llm_tr.update(ms=round(1000 * (time.perf_counter() - tb)), reason=f"LLM unavailable: {e}")
                parse["llm_reason"] = llm_tr["reason"]
            if ext is not None and (ext["ok"] or not parse["ok"]):
                parse = {"ok": ext["ok"], "intent": ext.get("command") or "extract", "route": "llm", "reason": llm_tr["reason"], "transcript": text}
                res = self.s.handle_extraction(ext, source, text, {"audio": audio, "llm_raw": raw})
            else:          # grammar parsed but the validator refused it and the LLM found nothing better: report the grammar's refusal
                res = self.s.handle_parse(parse, source, text, {"audio": audio, "route": "grammar"})
        else:
            res = self.s.handle_parse(parse, source, text, {"audio": audio, "route": parse.get("route")})
        t3 = time.perf_counter()
        self._trace(text, source, audio, parse, grammar_parse, llm_tr, res, stt_s, stt_wait_s, partial_text, t3 - ta, t3 - t0)
        self.partial = {"seq": self.partial["seq"], "text": "", "audio_s": 0.0, "ms": None}
        self.last = {"transcript": text, "route": parse.get("route"), "ok": res["ok"], "readback": res.get("readback"),
                     "reason": res.get("reason"), "audio": audio,
                     "ms": {"stt": round(1000 * stt_s) if stt_s is not None else None,
                            "grammar": round(1000 * (tb - ta), 1),
                            "llm": round(1000 * llm_s) if llm_s is not None else None,
                            "release_to_preview": round(1000 * (t3 - t0))}}
        self.s._bump()
        if self.speak:
            self.say(res.get("readback") if res["ok"] else "not understood")
        return self.last

    def _try_multi(self, text: str):
        """Row mode: 'B1 3.29, B2 3.30, B3 3.31' -> one parse per board when every pair is a plain value; else None."""
        if self.s.mode != "row" or self.COMPLEX & set(text.lower().replace(",", " ").replace(".", " ").split()):
            return None
        segs = rowmode.split_pairs(text, self.s.idx)
        if not segs:
            return None
        out = []
        for sg in segs:
            p = grammar.parse(sg, self.s.idx)
            if not p["ok"] or p["intent"] != "value" or not p.get("boards") or p["boards"] == "all" or len(p["boards"]) != 1:
                return None
            p["route"] = "grammar"
            out.append(p)
        if len({(p.get("id") or "") for p in out}) > 1 and not all(p.get("id") for p in out):
            return None
        return out

    COMPLEX = {"except", "apart", "but", "however", "besides", "instead", "actually", "sorry", "wrong", "correction", "same", "rest",
               "others", "remaining", "traced", "found", "because", "which", "while", "although"}

    def simple(self, text: str, parse: dict) -> bool:
        """The grammar's answer is trusted without the LLM only for a short, plain utterance that also binds to the sheet."""
        words = text.lower().replace(",", " ").replace(".", " ").split()
        if parse["intent"] not in ("value", "pass", "fail", "note"):
            return True
        if len(words) > 14 or self.COMPLEX & set(words) or re.search(r"\.\s+\S", text.strip()) and parse["intent"] != "note":
            return False
        if parse["intent"] == "note":
            return True
        return bool(validate.bind(parse, self.s.script, self.s.ctx)["ok"])

    # ---- trace of one utterance ----------------------------------------------------------------
    @staticmethod
    def _fields(p: dict) -> dict:
        """The parsed fields the operator can check: board, ID, value, unit."""
        v = p.get("value") or {}
        unit = v.get("unit") or v.get("prefix")
        b = p.get("boards")
        pairs = p.get("pairs")
        return {"pairs": [f"B{bd} = {v['number']}{(' ' + (v.get('unit') or v.get('prefix') or '')) if v.get('unit') or v.get('prefix') else ''}"
                          if v.get("kind") == "number" else f"B{bd} = {v.get('kind')}" for bd, v in pairs] if pairs else None,
                "intent": p.get("intent"),
                "board": "all" if b == "all" else (", ".join(f"B{x}" for x in b) if b else None),
                "id": p.get("id") or (", ".join(p["id_candidates"]) + " (candidates)" if p.get("id_candidates") else None),
                "row_no": p.get("row_no"), "value_kind": v.get("kind"),
                "number": v.get("number"), "unit": unit,
                "text": v.get("text"), "yn": v.get("values")}

    def _trace(self, text, source, audio, parse, grammar_parse, llm_tr, res, stt_s, stt_wait_s, partial_text, proc_s, total_s):
        self._trace_n += 1
        used = parse.get("route") if parse.get("ok") else None
        verdict = res.get("verdict")
        if verdict is None:
            intent = parse.get("intent")
            verdict = (f"command {intent}: " + (res.get("readback") or "")) if res["ok"] else f"command failed: {res.get('readback')}"
        tr = {"n": self._trace_n, "t": now_iso(), "source": source, "audio": os.path.basename(audio) if audio else None,
              "transcript": text, "last_partial": partial_text or None,
              "stt_ms": round(1000 * stt_s) if stt_s is not None else None,
              "stt_wait_ms": round(1000 * stt_wait_s) if stt_wait_s else None,
              "parser": used or "none",
              "grammar": {"ok": bool(grammar_parse["ok"]), "reason": grammar_parse.get("reason"),
                          "fields": self._fields(grammar_parse) if grammar_parse["ok"] else None},
              "llm": ({**llm_tr, "fields": self._fields(parse) if used == "llm" else None} if llm_tr else None),
              "validator": {"ok": bool(res["ok"]), "verdict": verdict, "reason": res.get("reason")},
              "targets": res.get("targets") or [], "readback": res.get("readback"), "row": res.get("row_trace"),
              "utt": next((t.get("utt") for t in res.get("targets") or [] if t.get("utt")), None),
              "processing_ms": round(1000 * proc_s), "release_to_preview_ms": round(1000 * total_s)}
        self.traces.append(tr)

    def say(self, text: str):
        if not text:
            return
        try:
            if self._say and self._say.poll() is None:
                self._say.terminate()
            self._say = subprocess.Popen(["say", "-r", "200", text.replace("Ω", " ohm")])
        except OSError:
            pass


def list_clips(path: str) -> list[str]:
    if os.path.isdir(path):
        return sorted(glob.glob(os.path.join(path, "*.wav")))
    with open(path, encoding="utf-8") as f:
        base = os.path.dirname(os.path.abspath(path))
        return [l.strip() if os.path.isabs(l.strip()) else os.path.join(base, l.strip()) for l in f if l.strip() and not l.startswith("#")]
