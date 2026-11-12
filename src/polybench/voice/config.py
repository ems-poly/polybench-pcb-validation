"""Voice configuration: one JSON file, defaults for anything missing (docs/VOICE.md §3.5)."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

# Default LLM : a 7-9B instruction model so the tool runs on lower-spec laptops. qwen3:8b (Ollama library tag,
# 5.2 GB, JSON-schema output, `think: false`). Larger installed models (gemma4:26b, the PoC's model) stay selectable.
DEFAULT_LLM = "qwen3:8b"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}

DEFAULTS: dict = {
    "llm_enabled": True,
    "llm_model": DEFAULT_LLM,
    "ollama_url": "http://127.0.0.1:11434",
    "stt_model": "mlx-community/whisper-large-v3-turbo",
    "stt_backend": "whisper",          # "whisper" | "sidecar"
    "tts_enabled": True,               # "Voice readouts" switch (UIR-13). Until 21 Oct 2026 this was the inverse of "Mute replies": same key, same meaning
    "tts_ack": True,                   # "Voice acknowledgements": short confirmations (accepted, rejected ...) are spoken; off = readouts only
    "tts_backend": "say",              # "say" | "log"
    "tts_voice": "",
    "tts_rate": 200,
    "grace_s": 3.0,
    "hf_gap_s": 0.8,
    "own_readout_tail_s": 0.5,         # TBR (UIR-37, proposed): hands-free input is ignored while the app speaks and this long after; the maintainer closes it at the bench
    "ptt_key": "Backquote",
    "row_mode": True,
    "confirm_simulated": True,         # a simulated action started by voice still needs "confirm" (off: only real actions do)
    "confirm_timeout_s": 10.0,         # seconds the spoken "confirm" stays valid
    "input_device": None,
}
_CHOICES = {"stt_backend": ("whisper", "sidecar"), "tts_backend": ("say", "log")}


def path() -> Path:
    env = os.environ.get("POLYBENCH_VOICE_CONFIG")
    return Path(env).expanduser() if env else Path("~/.config/polybench/voice.json").expanduser()


def loopback_only(url: str) -> str:
    """Ollama must run on this Mac: http on 127.0.0.1, localhost or ::1. Anything else raises ValueError (nothing leaves the Mac)."""
    u = urlsplit(str(url).strip())
    host = (u.hostname or "").lower()
    ok = u.scheme == "http" and (host in LOOPBACK or _is_loopback_ip(host))
    if not ok:
        raise ValueError(f"ollama_url must be local (http://127.0.0.1, localhost or [::1]); refused: {url}")
    return str(url).strip().rstrip("/")


def _is_loopback_ip(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _coerce(k: str, v):
    d = DEFAULTS[k]
    if k == "ollama_url":
        return loopback_only(v)
    if k == "input_device":
        return None if v in (None, "") else v
    if isinstance(d, bool):
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)
    if isinstance(d, int):
        return int(v)
    if isinstance(d, float):
        return float(v)
    v = str(v)
    if k in _CHOICES and v not in _CHOICES[k]:
        raise ValueError(f"{k} must be one of {', '.join(_CHOICES[k])}")
    return v


def validate(patch: dict) -> dict:
    """Coerce a patch; unknown keys raise ValueError."""
    out = {}
    for k, v in patch.items():
        if k not in DEFAULTS:
            raise ValueError(f"unknown voice setting '{k}'")
        out[k] = _coerce(k, v)
    for k, lo, hi in (("grace_s", 0, 30), ("confirm_timeout_s", 3, 60), ("hf_gap_s", 0.3, 3.0), ("own_readout_tail_s", 0.0, 3.0), ("tts_rate", 80, 400)):
        if k in out:
            out[k] = min(hi, max(lo, out[k]))
    return out


def load() -> dict:
    cfg = dict(DEFAULTS)
    p = path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in DEFAULTS:
                try:
                    cfg[k] = _coerce(k, v)
                except (ValueError, TypeError):
                    pass
    return cfg


def save(cfg: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: cfg[k] for k in DEFAULTS}, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
