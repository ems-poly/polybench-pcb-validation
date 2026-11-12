"""UIR-12: the one rule for everything spoken (voice/tts.py spoken_summary), and UIR-13 acknowledgement vs readout routing."""
import hashlib

import pytest

from polybench.voice import tts as T

SHA = hashlib.sha256(b"DEMO").hexdigest()          # 64 hex chars
CHIP = "a1b2c3d4e5f60718293a4b5c6d7e8f90"           # 128-bit chip ID, 32 hex chars
SERIAL = "123456789"                                # 9 digits
LONG_TITLE = ("DEMO rail check at power up, measured across the load with the board warm and the lid closed, "
              "repeated for every channel in the batch before moving on to the next step of the plan")


def test_constants_are_the_proposed_tbr_values():
    assert T.LONG_VALUE_CHARS == 12 and T.MAX_SPOKEN_WORDS == 20


@pytest.mark.parametrize("src,want", [
    ("U01, 3V3 rail (acceptance: within 3 percent of nominal).", "U01, 3V3 rail."),
    ("Heard S1 0.2 V [flag: outside limit]", "Heard S1 0.2 V"),
    ("Outer (inner (nested) text) done", "Outer done"),
    ("Dangling ( bracket text", "Dangling bracket text"),
])
def test_r1_no_bracketed_text(src, want):
    out = T.spoken_summary(src)
    assert out == want
    assert not any(c in out for c in "()[]{}")


def test_r2_sha256_and_chip_id_are_value_on_screen():
    assert T.spoken_summary(f"Expected {SHA}") == "Expected value on screen"
    assert T.spoken_summary(f"U05 chip ID 0x{CHIP}, read.") == "U05 chip ID value on screen, read."
    assert T.spoken_summary(f"{CHIP} {SHA}") == "value on screen"          # two in a row collapse to one


def test_r2_short_values_and_plain_words_are_kept():
    assert T.spoken_summary("S1 reads 0x1101070A, 3.30 V") == "S1 reads 0x1101070A, 3.30 V"
    assert T.spoken_summary("Characterisation of everything") == "Characterisation of everything"      # long but no digit
    assert T.spoken_summary("S01 U02 3V3") == "S01 U02 3V3"


def test_r2_nine_digit_serial_follows_the_threshold():
    # 9 characters is below the proposed 12: it is spoken. To silence it the TBR threshold must be 8 or less.
    assert T.spoken_summary(f"Serial {SERIAL}") == f"Serial {SERIAL}"
    assert T.spoken_summary(f"Serial {SERIAL}", long_chars=8) == "Serial value on screen"
    assert T.spoken_summary("Serial 1234567890123") == "Serial value on screen"


def test_r3_at_most_m_words_and_cut_at_a_sentence():
    out = T.spoken_summary(LONG_TITLE)
    assert len(out.split()) <= T.MAX_SPOKEN_WORDS
    two = "One two three four five six seven eight nine ten. " + "word " * 30
    assert T.spoken_summary(two) == "One two three four five six seven eight nine ten."
    assert len(T.spoken_summary("w " * 100, max_words=5).split()) == 5
    assert T.spoken_summary("Short reply.") == "Short reply."
    assert T.spoken_summary("") == ""


def test_say_logs_full_reply_speaks_summary(tmp_path):
    cfg = {"tts_enabled": True, "tts_backend": "log", "tts_voice": "", "tts_rate": 200}
    t = T.Tts(lambda: cfg)
    t.say(f"U01 (acceptance {SHA}) is fine")
    assert t.log[-1].startswith("U01 (acceptance") and t.spoken[-1] == "U01 is fine"


def test_acknowledgements_switch_only_silences_acks():
    cfg = {"tts_enabled": True, "tts_ack": False, "tts_backend": "log", "tts_voice": "", "tts_rate": 200}
    t = T.Tts(lambda: cfg)
    t.say("Accepted.", kind="ack")
    t.say("S1 U01 0.2 volts.", kind="readout")
    assert t.spoken == ["S1 U01 0.2 volts."]
    cfg["tts_ack"] = True
    t.say("Accepted.", kind="ack")
    assert t.spoken[-1] == "Accepted."


def test_old_mute_value_keeps_its_meaning(tmp_path, monkeypatch):
    """A voice.json written before UIR-13 has tts_enabled (false = replies muted) and no tts_ack: it loads with the same meaning."""
    from polybench.voice import config as vc
    p = tmp_path / "old.json"
    p.write_text('{"tts_enabled": false}')
    monkeypatch.setenv("POLYBENCH_VOICE_CONFIG", str(p))
    c = vc.load()
    assert c["tts_enabled"] is False and c["tts_ack"] is True
    t = T.Tts(lambda: {**c, "tts_backend": "say"})
    assert not t.sound_on()
