"""Ported from the voice proof of concept (VAD part). The Engine-based tests are in test_capture.py.
Hands-free capture: VAD segmentation on synthetic clips, the silence gap, mute while the tool speaks, the simulator stream,
and the push-to-talk failure causes found in the 15 Oct audit (silent clips, hallucinations, lost key-up)."""
import numpy as np
import pytest

from polybench.voice import stt, vad

SR = vad.SR


def speech(dur, peak=0.15, f0=140.0, seed=0):
    """A speech-like burst: harmonics of f0 with a 4 Hz syllable envelope. Not speech, but it behaves like it for an energy VAD."""
    t = np.arange(int(dur * SR)) / SR
    x = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 12))
    env = 0.55 + 0.45 * np.sin(2 * np.pi * 4 * t) ** 2
    x = x * env
    return (x / np.abs(x).max() * peak * 1.4).astype(np.float32)


def room(dur, rms=0.004, seed=1):
    return vad.noise("pink", int(dur * SR), rms, np.random.default_rng(seed))


def stream(*parts):
    return np.concatenate([np.asarray(p, np.float32) for p in parts])


def run(sig, gap=0.8, muted=lambda i: False):
    seg = vad.Segmenter(vad.VadParams(gap_s=gap))
    out = []
    for i, b in enumerate(vad.blocks(sig)):
        u = seg.feed(b, muted=muted(i))
        if u is not None:
            out.append(u)
    return out, seg


def with_noise(sig, rms=0.004, seed=2):
    return sig + vad.noise("pink", len(sig), rms, np.random.default_rng(seed))


def test_two_utterances_are_cut_apart_and_timed():
    sig = with_noise(stream(np.zeros(2 * SR), speech(1.0), np.zeros(int(1.6 * SR)), speech(0.6), np.zeros(2 * SR)))
    us, _ = run(sig)
    assert len(us) == 2
    assert abs(us[0].t_start - 2.0) < 0.45 and abs(len(us[0].audio) / SR - 1.0) < 0.9     # pre-roll and tail around 1.0 s of speech
    assert us[0].voiced_s > 0.8 and us[1].voiced_s > 0.45
    assert us[1].t_start > 4.0


def test_release_comes_one_gap_after_the_end_of_speech():
    for gap in (0.5, 0.8, 1.2):
        sig = with_noise(stream(np.zeros(2 * SR), speech(1.0), np.zeros(3 * SR)))
        us, _ = run(sig, gap)
        assert len(us) == 1
        lat = us[0].t_emit - 3.0
        assert gap - 0.15 <= lat <= gap + 0.15, (gap, lat)


def test_a_pause_shorter_than_the_gap_does_not_split_and_a_longer_one_does():
    sig = with_noise(stream(np.zeros(2 * SR), speech(0.8), np.zeros(int(0.6 * SR)), speech(0.8), np.zeros(2 * SR)))
    assert len(run(sig, 0.8)[0]) == 1
    assert len(run(sig, 0.4)[0]) == 2


def test_noise_only_makes_no_utterance_even_with_clicks_and_hum():
    rng = np.random.default_rng(3)
    sig = vad.noise("fan", 40 * SR, 0.01, rng)
    for t in np.linspace(3 * SR, 38 * SR, 14).astype(int):          # probe taps
        n = int(0.008 * SR)
        sig[t:t + n] += (rng.standard_normal(n) * np.exp(-np.arange(n) / (0.002 * SR)) * 0.4).astype(np.float32)
    us, _ = run(sig)
    assert us == []
    us, _ = run(vad.noise("hum", 30 * SR, 0.01, rng))
    assert us == []


def test_speech_over_a_loud_steady_room_is_still_found():
    sig = stream(vad.noise("fan", 3 * SR, 0.02, np.random.default_rng(5)), speech(1.0, peak=0.4))
    sig = stream(sig, np.zeros(3 * SR))
    sig = sig + vad.noise("fan", len(sig), 0.02, np.random.default_rng(6))
    assert len(run(sig)[0]) == 1


def test_room_chatter_far_below_the_operator_does_not_hold_the_utterance_open():
    chatter = speech(8.0, peak=0.15 * 0.2, f0=190.0)                  # a conversation 14 dB down, running all the time
    sig = stream(np.zeros(2 * SR), speech(1.0), np.zeros(6 * SR))
    sig = with_noise(sig) + np.concatenate([np.zeros(SR), chatter, np.zeros(len(sig) - 9 * SR)])[:len(sig)]
    us, _ = run(sig)
    assert len(us) >= 1 and us[0].t_emit - 3.0 < 1.5


def test_click_and_too_quiet_murmur_are_discarded_not_sent():
    sig = with_noise(stream(np.zeros(2 * SR), speech(0.05), np.zeros(2 * SR), speech(1.0, peak=0.015), np.zeros(2 * SR)))
    us, seg = run(sig)
    assert us == []


def test_mute_drops_everything_while_the_tool_speaks_and_a_moment_after():
    sig = with_noise(stream(np.zeros(2 * SR), speech(1.2), np.zeros(3 * SR)))
    t0, t1 = int(2 * SR / vad.BLOCK), int(3.4 * SR / vad.BLOCK)        # muted across the whole utterance
    us, seg = run(sig, muted=lambda i: t0 - 2 <= i <= t1)
    assert us == []
    # muted from the middle of an utterance: that utterance is dropped and counted
    us, seg = run(sig, muted=lambda i: i >= int(2.5 * SR / vad.BLOCK) and i <= t1)
    assert seg.muted_drops >= 1 and all(u.voiced_s < 0.9 for u in us)
    # muted for only the first half: the rest of the speech must not be picked up as a new utterance during the cool-down
    mid = int(2.5 * SR / vad.BLOCK)
    us, seg = run(sig, muted=lambda i: i <= mid)
    assert all(u.voiced_s < 0.9 for u in us)
