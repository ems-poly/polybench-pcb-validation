"""Ported from the voice proof of concept (the Engine-based part, now on Capture): hands-free
queue, mute while the tool speaks, PTT failure causes (silent clips, hallucinations, lost key-up, post-roll), plus the new rule that
the microphone is open only while voice is enabled and the page is visible."""
import time

import numpy as np
import pytest

from polybench.voice import capture as capmod, config as vcfg, vad

from conftest import speech

SR = vad.SR


def with_noise(sig, rms=0.004, seed=2):
    return sig + vad.noise("pink", len(sig), rms, np.random.default_rng(seed))


def stream(*parts):
    return np.concatenate([np.asarray(p, np.float32) for p in parts])


def wait(cond, t=8.0):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class FakeWhisper:
    def __init__(self, text="0.62 volts", model=vcfg.DEFAULTS["stt_model"]):
        self.text, self.calls, self.model, self.prompt, self.warm = text, 0, model, None, True

    def transcribe(self, audio):
        self.calls += 1
        return self.text


class Rec:
    level = 0.0

    def __init__(self, audio=None):
        self.audio = np.zeros(0, np.float32) if audio is None else audio
        self.started = self.closed = False
        self.tap = None

    def start(self):
        self.started = True

    def snapshot(self):
        return self.audio

    def stop(self):
        self.stopped_at = time.perf_counter()
        return self.audio

    def cancel(self):
        pass

    def close(self):
        self.closed = True


class Harness:
    def __init__(self, tmp_path):
        self.texts, self.drops, self.speaking_now = [], [], False
        self.opened: list[Rec] = []
        self.cfg = dict(vcfg.DEFAULTS)
        self.audio = np.zeros(0, np.float32)
        self.cap = capmod.Capture(lambda: self.cfg, lambda: self.speaking_now, self._text, self._drop,
                                  cache_dir=str(tmp_path / "clips"), recorder_factory=self._factory, tail_s=0.02)
        self.cap.whisper = FakeWhisper()

    def _factory(self, device):
        r = Rec(self.audio)
        self.opened.append(r)
        return r

    def _text(self, text, source, info):
        self.texts.append((text, source, info))
        return {"text": text}

    def _drop(self, why, text, source, info):
        self.drops.append((why, text, source, info))
        return {"drop": why}

    def go(self, hf=False):
        self.cap.set_enabled(True)
        self.cap.set_visible(True)
        if hf:
            self.cap.set_handsfree(True)

    def press(self):
        assert self.cap.ptt_start()
        assert self.cap.ptt_stop()
        assert wait(lambda: not self.cap.busy and not self.cap._fin.locked())


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def test_microphone_is_open_only_while_enabled_and_visible(h):
    cap = h.cap
    cap.set_enabled(True)
    assert not h.opened and cap.rec is None             # page not visible yet
    cap.set_visible(True)
    assert len(h.opened) == 1 and cap.rec is h.opened[0]
    cap.set_visible(False)
    assert h.opened[0].closed and cap.rec is None
    cap.set_visible(True)
    assert len(h.opened) == 2
    cap.set_enabled(False)
    assert h.opened[1].closed and cap.rec is None


def test_missing_microphone_is_reported_and_does_not_raise(tmp_path):
    def boom(device):
        raise OSError("no input device")
    cap = capmod.Capture(lambda: dict(vcfg.DEFAULTS), lambda: False, lambda *a: {}, lambda *a: {}, recorder_factory=boom, cache_dir=str(tmp_path))
    cap.set_enabled(True)
    cap.set_visible(True)
    assert cap.rec is None and "no input device" in cap.mic_error
    assert cap.ptt_start() is False


def test_hiding_the_page_stops_a_press_in_progress(h):
    h.go()
    assert h.cap.ptt_start()
    h.cap.set_visible(False)
    assert not h.cap.recording


def test_handsfree_tap_follows_the_open_recorder(h):
    h.go(hf=True)
    assert h.cap.rec.tap == h.cap.hf_block
    h.cap.set_handsfree(False)
    assert h.cap.rec.tap is None


def test_handsfree_utterances_reach_stt_and_the_callback(h):
    h.go(hf=True)
    h.cap.seg.p.warmup_s = 1.0
    sig = with_noise(stream(np.zeros(2 * SR), speech(1.0), np.zeros(int(1.6 * SR)), speech(0.8), np.zeros(2 * SR)))
    for b in vad.blocks(sig):
        h.cap.hf_block(b, muted=False)
    assert wait(lambda: len(h.texts) == 2 and not h.cap.busy)
    assert h.cap.whisper.calls == 2 and {t[1] for t in h.texts} == {"handsfree"} and h.texts[0][2]["stt_ms"] is not None
    assert h.cap.hf["captured"] == 2


def test_nothing_is_captured_while_the_tool_is_speaking(h):
    h.go(hf=True)
    h.speaking_now = True
    for b in vad.blocks(with_noise(stream(np.zeros(2 * SR), speech(1.2), np.zeros(2 * SR)))):
        h.cap.hf_block(b)
    assert h.cap.hf["captured"] == 0 and h.cap.hfq.qsize() == 0 and h.cap.muted() is True


def test_cue_style_mute_for_a_moment(h):
    h.cap.mute_for(0.3)
    assert h.cap.muted() is True
    time.sleep(0.8)
    assert h.cap.muted() is False


def test_handsfree_is_off_while_the_key_is_held_and_ptt_still_works(h):
    h.go(hf=True)
    h.cap.recording = True
    for b in vad.blocks(with_noise(stream(np.zeros(2 * SR), speech(1.0), np.zeros(2 * SR)))):
        h.cap.hf_block(b)
    assert h.cap.hf["captured"] == 0


def test_a_press_in_silence_is_reported_not_sent_to_whisper(h):
    h.audio = with_noise(np.zeros(2 * SR), 0.003)
    h.go()
    h.press()
    assert h.cap.whisper.calls == 0 and not h.texts
    assert h.drops and "no speech" in h.drops[0][0]
    assert any(e["event"] == "dropped" for e in h.cap.audit)


def test_whisper_hallucination_on_a_quiet_clip_is_dropped_and_logged(h):
    h.audio = with_noise(speech(1.5, peak=0.04))
    h.cap.whisper.text = "Thank you."
    h.go()
    h.press()
    assert h.cap.whisper.calls == 1 and not h.texts
    assert h.drops[0][1] == "Thank you."


def test_every_press_is_in_the_audit_log_with_levels(h):
    h.audio = with_noise(speech(2.0, peak=0.3))
    h.go()
    h.press()
    p = [e for e in h.cap.audit if e["event"] == "ptt"]
    assert p and p[0]["audio_s"] == pytest.approx(2.0, abs=0.1) and p[0]["peak_rms"] > 0.1 and "floor_rms" in p[0]
    assert h.texts[0][0] == "0.62 volts" and h.texts[0][1] == "ptt" and h.texts[0][2]["audio"].endswith(".wav")


def test_too_short_press_is_logged_and_dropped(h):
    h.audio = np.zeros(1000, np.float32)
    h.go()
    h.press()
    assert any(e.get("reason") == "too short" for e in h.cap.audit) and "too short" in h.drops[0][0]


def test_lost_key_up_is_closed_by_the_watchdog(h):
    h.audio = with_noise(speech(2.0, peak=0.3))
    h.go()
    h.cap.ptt_start(beats=True)
    h.cap._beat -= 5.0
    assert wait(lambda: not h.cap.recording, 3)
    assert wait(lambda: len(h.texts) == 1 and not h.cap.busy)
    assert any(e.get("result") == "auto_stop" for e in h.cap.audit)


def test_no_watchdog_for_a_client_that_sends_no_heartbeat(h):
    h.audio = with_noise(speech(2.0, peak=0.3))
    h.go()
    h.cap.ptt_start()
    time.sleep(1.2)
    assert h.cap.recording
    h.cap.ptt_cancel()


def test_post_roll_keeps_the_last_word(h):
    h.audio = with_noise(speech(1.0, peak=0.3))
    h.go()
    h.cap.tail_s = 0.25
    h.cap.ptt_start()
    t = time.perf_counter()
    h.cap.ptt_stop()
    assert wait(lambda: not h.cap.busy and not h.cap._fin.locked())
    assert h.cap.rec.stopped_at - t >= 0.24


def test_sidecar_backend_reads_the_text_file_and_needs_no_whisper(h, tmp_path):
    from polybench.voice import stt
    h.cfg["stt_backend"] = "sidecar"
    h.cap.whisper = None
    w = tmp_path / "a.wav"
    stt.write_wav(str(w), speech(1.0))
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    assert h.cap.feed_wav(str(w)) == {"text": "hello"} and h.texts[0][1] == "clip"
    assert h.cap.stt_status()["ok"]


def test_whisper_prompt_is_set_and_model_stays_hot(h):
    h.cap.prompt = lambda: "ids S01"
    h.cap.transcribe(np.zeros(SR, np.float32), None)
    h.cap.transcribe(np.zeros(SR, np.float32), None)
    assert h.cap.whisper.prompt == "ids S01" and h.cap.whisper.calls == 2
