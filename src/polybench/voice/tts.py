"""Spoken replies through macOS `say`. Backend "log" makes no sound (CI). The text is always returned and journalled by the caller."""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from typing import Callable

_UNITS = [(r"kOhms?", "kilo ohms"), (r"MOhms?", "mega ohms"), (r"Ohms?", "ohms"), (r"mA", "milliamps"), (r"uA", "microamps"),
          (r"mV", "millivolts"), (r"kHz", "kilohertz"), (r"MHz", "megahertz"), (r"Hz", "hertz"), (r"V", "volts"), (r"A", "amps")]


# UIR-12 (TBR, proposed values; the maintainer closes both at the bench): the one rule for everything spoken.
LONG_VALUE_CHARS = 12        # a token longer than this that holds a digit (hash, hex, chip ID, serial) is spoken as ON_SCREEN
MAX_SPOKEN_WORDS = 20        # a spoken readout is cut to at most this many words; the full text stays on screen
ON_SCREEN = "value on screen"
_BRACKETS = re.compile(r"\([^()]*\)|\[[^\[\]]*\]|\{[^{}]*\}")
_TOKEN = re.compile(r"(?<![\w.])[0-9A-Za-z][0-9A-Za-z_-]*(?!\w)")


def strip_brackets(text: str) -> str:
    """The one bracket rule (UIR-12): text in (), [] or {} is removed, innermost first so nested brackets go whole; a stray bracket
    is replaced by a space. Every spoken path (spoken_summary, spoken_name) uses this."""
    t = text
    for _ in range(8):
        t2 = _BRACKETS.sub(" ", t)
        if t2 == t:
            break
        t = t2
    return re.sub(r"[()\[\]{}]", " ", t)


def spoken_summary(text: str, long_chars: int | None = None, max_words: int | None = None) -> str:
    """What is actually said: no text in brackets, long hashes/IDs/serials replaced by 'value on screen', at most
    MAX_SPOKEN_WORDS words (cut at the last sentence end that fits, else at the word limit)."""
    n = LONG_VALUE_CHARS if long_chars is None else long_chars
    m = MAX_SPOKEN_WORDS if max_words is None else max_words
    t = strip_brackets(str(text))
    t = _TOKEN.sub(lambda mo: ON_SCREEN if len(mo.group(0)) > n and re.search(r"\d", mo.group(0)) else mo.group(0), t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    t = re.sub(rf"(?:{ON_SCREEN}[ ,]*){{2,}}", ON_SCREEN + " ", t).strip()
    w = t.split(" ") if t else []
    if len(w) > m:
        cut = " ".join(w[:m])
        k = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        t = cut[:k + 1] if k > 0 else cut.rstrip(",;:")
    return t


def speakable(text: str) -> str:
    """Units after a number are read out ('3.4 V' -> '3.4 volts'); comparison signs become words."""
    t = text.replace(">=", "at least ").replace("<=", "at most ").replace("Ω", " ohm")
    # 19 Oct, bench session: never read a hash, UID or long hex value digit by digit, and drop the limit-class tag (HARD, GUESS, TBR).
    t = re.sub(r"(?<=\S)(HARD|GUESS|TBR|RECORD)\b", "", t)
    t = re.sub(r"\b(?:0x)?[0-9a-fA-F]{16,}\b", ON_SCREEN, t)
    hexes = re.findall(r"\b0x[0-9a-fA-F]+\b", t)           # keep 0x1101070A whole: its trailing "0A" is not "0 amps"
    for i, h in enumerate(hexes):
        t = t.replace(h, f"\x00{i}\x00", 1)
    for pat, word in _UNITS:
        t = re.sub(rf"(?<=\d) ?{pat}\b", " " + word, t)
    for i, h in enumerate(hexes):
        t = t.replace(f"\x00{i}\x00", h, 1)
    return t


SPOKEN_CONTENT_URLS = (
    "x-apple.systempreferences:com.apple.Accessibility-Settings.extension?SpokenContent",   # macOS 13+
    "x-apple.systempreferences:com.apple.preference.universalaccess?SpeakableItems",        # older
    "x-apple.systempreferences:com.apple.preference.universalaccess",                       # Accessibility pane
)
TEST_PHRASE = "Polybench voice test. S 1, zero point two volts."
_GOOD = re.compile(r"\((Premium|Enhanced)\)", re.I)


def is_natural(name: str) -> bool:
    """Premium and Enhanced voices are the downloadable high-quality ones; the rest are the basic compact voices."""
    return bool(_GOOD.search(name))


def open_speech_settings() -> dict:
    """Open System Settings at Spoken Content with a fixed URL (no input); fall back to the Accessibility pane."""
    for url in SPOKEN_CONTENT_URLS:
        try:
            r = subprocess.run(["open", url], capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            return {"ok": True, "url": url}
    return {"ok": False, "url": None}


class Tts:
    def __init__(self, get_cfg: Callable[[], dict]):
        self.cfg = get_cfg
        self.muted = False                  # operator toggle: replies stay text only
        self.proc: subprocess.Popen | None = None
        self.log: list[str] = []            # the replies, newest last (full text, as on screen)
        self.spoken: list[str] = []         # what was said (or would have been) after spoken_summary, newest last
        self._lock = threading.Lock()
        self._voices: tuple[float, list[str]] | None = None

    def available(self) -> bool:
        return shutil.which("say") is not None

    def sound_on(self) -> bool:
        c = self.cfg()
        return bool(c["tts_enabled"]) and not self.muted and c["tts_backend"] == "say" and self.available()

    def say(self, text: str, voice: str | None = None, kind: str = "readout") -> None:
        """kind 'ack' = a short acknowledgement (accepted, rejected, cancelled ...): spoken only while Voice acknowledgements is on."""
        if not text:
            return
        self.log.append(text)
        del self.log[:-50]
        if kind == "ack" and not self.cfg().get("tts_ack", True):
            return
        text = spoken_summary(text)
        if not text:
            return
        self.spoken.append(text)
        del self.spoken[:-50]
        if not self.sound_on():
            return
        c = self.cfg()
        cmd = ["say", "-r", str(int(c["tts_rate"]))]
        v = self.effective_voice() if voice is None else voice
        if v:
            cmd += ["-v", v]
        with self._lock:
            self.stop()
            try:
                self.proc = subprocess.Popen(cmd + [speakable(text)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self.proc = None

    def speaking(self) -> bool:
        p = self.proc
        return p is not None and p.poll() is None

    def stop(self) -> None:
        p = self.proc
        if p is not None and p.poll() is None:
            p.terminate()

    def when_done(self, cb: Callable[[], None]) -> None:
        """Call cb (on another thread) once the current reply has finished; at once when nothing is playing."""
        p = self.proc

        def run():
            if p is not None:
                try:
                    p.wait()
                except Exception:
                    pass
            cb()
        threading.Thread(target=run, daemon=True, name="tts-done").start()

    def voices(self) -> list[str]:
        """Installed voices, Premium and Enhanced first (cached for a minute: status polls this)."""
        now = time.monotonic()
        if self._voices and now - self._voices[0] < 60:
            return self._voices[1]
        if not self.available():
            return []
        try:
            out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        names = [m.group(1).strip() for l in out.splitlines() if (m := re.match(r"^(.+?)\s{2,}\S+\s+#", l))]
        names = [n for n in names if is_natural(n)] + [n for n in names if not is_natural(n)]
        self._voices = (now, names)
        return names

    def effective_voice(self) -> str:
        """The configured voice; with the setting empty, the first Premium/Enhanced voice if one is installed, else ''
        (the macOS System voice, which `say` can only use for a basic voice: Siri voices are not available to other apps)."""
        v = self.cfg()["tts_voice"]
        return v or self._default_voice()

    def _default_voice(self) -> str:
        first = next(iter(self.voices()), "")
        return first if is_natural(first) else ""

    def test(self, voice: str | None = None) -> dict:
        """One short phrase through the current setting (or `voice`, an installed name or '' for the setting's default).
        Silent while the operator has muted the replies."""
        use = voice if isinstance(voice, str) and (voice == "" or voice in self.voices()) else self.cfg()["tts_voice"]
        use = use or self._default_voice()
        if not self.sound_on():
            return {"spoken": False, "muted": self.muted or not self.cfg()["tts_enabled"], "voice": use}
        self.say(TEST_PHRASE, voice=use)
        return {"spoken": True, "muted": False, "voice": use}

    def status(self) -> dict:
        c = self.cfg()
        ok = (c["tts_backend"] == "log") or self.available()
        detail = "log only (no sound)" if c["tts_backend"] == "log" else ("" if ok else "macOS 'say' not found")
        if self.muted:
            detail = "muted"
        use = self.effective_voice() if c["tts_backend"] == "say" and self.available() else c["tts_voice"]
        warn = ""
        if c["tts_backend"] == "say" and self.available() and not c["tts_voice"] and not use:
            warn = "Only basic voices are installed"
        return {"ok": ok, "voice": c["tts_voice"], "voice_in_use": use, "label": use or "System voice", "warn": warn,
                "rate": c["tts_rate"], "detail": detail}
