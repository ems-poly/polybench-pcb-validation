"""UIR-37: hands-free input is half-duplex. While a readout plays, and for own_readout_tail_s after it (TBR 0.5 s), the microphone is ignored
and an utterance dropped that way is journaled as 'dropped: own readout'. HOST-ONLY: a fake TTS process on a fake clock, no audio, no `say`.
Whether a real microphone hears a real speaker is UNVALIDATED until the bench session."""
import time

import numpy as np
import pytest

from polybench.voice import capture as capmod
from polybench.voice import config as vcfg
from polybench.voice import vad
from polybench.voice.tts import Tts

from conftest import speech
from test_capture import FakeWhisper, Rec, stream, wait

SR, BLK = vad.SR, vad.BLOCK
DT = BLK / SR


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class FakeProc:
    """Stands in for the `say` process: running until the fake clock reaches `end`."""
    def __init__(self, clock, dur):
        self.clock, self.end = clock, clock.t + dur

    def poll(self):
        return None if self.clock.t < self.end else 0

    def terminate(self):
        self.end = self.clock.t


class Rig:
    def __init__(self, tmp_path, tail=None):
        self.clock = Clock()
        self.cfg = {**vcfg.DEFAULTS, "tts_backend": "log"}
        if tail is not None:
            self.cfg["own_readout_tail_s"] = tail
        self.tts = Tts(lambda: self.cfg)
        self.texts, self.drops = [], []
        self.cap = capmod.Capture(lambda: self.cfg, self.tts.speaking, lambda t, s, i: self.texts.append((t, s)) or {}, self._drop,
                                  cache_dir=str(tmp_path / "clips"), recorder_factory=lambda d: Rec(), tail_s=0.01, clock=self.clock)
        self.cap.whisper = FakeWhisper()
        self.cap.set_enabled(True)
        self.cap.set_visible(True)
        self.cap.set_handsfree(True)
        self.cap.seg.p.warmup_s = 0.5
        self.listen(np.zeros(int(0.8 * SR), np.float32))        # the room, so the segmenter has its floor

    def _drop(self, why, text, source, info):
        self.drops.append((why, source, info))
        return {}

    def speak(self, dur):
        self.tts.proc = FakeProc(self.clock, dur)

    def listen(self, sig):
        """Feed the microphone block by block, the fake clock moving 32 ms per block."""
        sig = np.asarray(sig, np.float32)
        sig = sig + vad.noise("pink", len(sig), 0.004, np.random.default_rng(3))
        for b in vad.blocks(sig):
            self.clock.t += DT
            self.cap.hf_block(b)

    def settle(self):
        assert wait(lambda: self.cap.hfq.qsize() == 0 and not self.cap.busy)
        time.sleep(0.05)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_input_while_a_readout_plays_is_dropped_and_journaled(rig):
    rig.speak(4.0)
    rig.listen(stream(np.zeros(SR // 2), speech(1.0), np.zeros(2 * SR)))        # the app's own voice, heard 0.5 s into the readout
    rig.listen(np.zeros(2 * SR))                                                 # the readout ends, the tail passes
    rig.settle()
    assert rig.texts == [] and rig.cap.whisper.calls == 0 and rig.cap.hf["captured"] == 0
    assert [d[0] for d in rig.drops] == ["dropped: own readout"] and rig.drops[0][1] == "handsfree"
    assert rig.drops[0][2]["voiced_s"] > 0.5


def test_input_inside_the_tail_after_the_readout_is_dropped(rig):
    rig.speak(0.2)
    rig.listen(np.zeros(int(0.3 * SR)))                                           # readout over at +0.2 s; now +0.3 s: 0.1 s into the 0.5 s tail
    assert rig.cap.muted() is True
    rig.listen(stream(speech(0.3), np.zeros(2 * SR)))                             # speech inside the tail
    rig.settle()
    assert rig.texts == [] and [d[0] for d in rig.drops] == ["dropped: own readout"]


def test_input_after_the_tail_is_accepted(rig):
    rig.speak(0.2)
    rig.listen(np.zeros(int(0.2 * SR) + int(0.6 * SR)))                           # readout, then 0.6 s of silence: past the 0.5 s tail
    assert rig.cap.muted() is False
    rig.listen(stream(speech(1.0), np.zeros(2 * SR)))
    rig.settle()
    assert [t[0] for t in rig.texts] == ["0.62 volts"] and rig.drops == [] and rig.cap.hf["captured"] == 1


def test_the_tail_is_exactly_the_setting(tmp_path):
    for tail, muted_after_0_4 in ((0.5, True), (0.3, False), (0.0, False)):
        r = Rig(tmp_path / f"t{tail}", tail=tail)
        r.speak(0.1)
        r.listen(np.zeros(int(0.5 * SR)))                                         # 0.4 s after the end of the readout
        assert r.cap.muted() is muted_after_0_4, tail


def test_a_quiet_readout_in_the_room_journals_nothing(rig):
    rig.speak(2.0)
    rig.listen(np.zeros(3 * SR))                                                  # nothing voiced reaches the microphone
    rig.settle()
    assert rig.drops == [] and rig.texts == []


def test_push_to_talk_while_speaking_is_accepted(tmp_path):
    """PTT is a deliberate key press: it stops the readout (controller.ptt_start) and the clip goes through; hands-free is off while the key is held."""
    r = Rig(tmp_path)
    r.speak(10.0)
    r.cap.set_handsfree(False)
    r.cap.rec.audio = stream(speech(1.0))
    assert r.cap.ptt_start() and r.cap.ptt_stop()
    assert wait(lambda: not r.cap.busy and not r.cap._fin.locked())
    assert [t[1] for t in r.texts] == ["ptt"] and r.drops == []


def test_controller_ptt_start_stops_the_readout(vc):
    stopped = []
    vc.tts.stop = lambda: stopped.append(1)
    vc.capture.rec = Rec()
    vc.ptt_start()
    assert stopped == [1]


def test_the_drop_is_journaled_not_spoken_and_not_acted_on(vc):
    n_spoken = len(vc.tts.spoken)
    e = vc.capture._drop("dropped: own readout", None, None, "handsfree", {"voiced_s": 0.9, "peak": 0.2})
    assert e["outcome"] == "dropped" and e["detail"] == "dropped: own readout" and e["reply"] == ""
    assert len(vc.tts.spoken) == n_spoken                                          # speaking the drop would feed the loop
    assert any(v["detail"] == "dropped: own readout" for v in vc.eng.state.voice)
    assert vc.eng.journal_hash()                                                    # it is in the journal
