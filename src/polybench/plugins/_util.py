"""Shared helpers for the Polybench plug-ins. No hardware, no third-party imports."""
from __future__ import annotations

import hashlib
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Callable


class PluginError(RuntimeError):
    """A failure the operator should read as is. The message says what to check."""


class ParamError(PluginError):
    """The protocol gave the action bad or missing params."""


def need(params: dict, key: str, kind: type | tuple | None = None) -> Any:
    if key not in params or params[key] in (None, ""):
        raise ParamError(f"missing param '{key}'")
    v = params[key]
    if kind is not None and not isinstance(v, kind):
        raise ParamError(f"param '{key}' must be {kind}, got {type(v).__name__}")
    return v


def to_int(v: Any, what: str = "value") -> int:
    """Accept 1000000, '1000000', '0x3F0', '3F0h' is not accepted."""
    if isinstance(v, bool):
        raise ParamError(f"{what}: not a number: {v!r}")
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        try:
            return int(v.strip(), 0)
        except ValueError:
            pass
    raise ParamError(f"{what}: not an integer: {v!r}")


def timeout_param(v: Any, what: str = "timeout_s") -> float:
    """Seconds allowed: a finite number greater than zero."""
    if isinstance(v, bool):
        raise ParamError(f"{what}: not a number: {v!r}")
    x = to_float(v, what)
    if x <= 0:
        raise ParamError(f"{what} must be greater than 0 (seconds), got {v!r}")
    return x


def retries_param(v: Any, what: str = "retries") -> int:
    n = to_int(v, what)
    if n < 0:
        raise ParamError(f"{what} must be 0 or more, got {v!r}")
    return n


def to_float(v: Any, what: str = "value") -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ParamError(f"{what}: not a number: {v!r}") from None
    if not math.isfinite(x):
        raise ParamError(f"{what}: not finite: {v!r}")
    return x


def hex_bytes(v: Any, what: str = "data") -> bytes:
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    if isinstance(v, list):
        return bytes(to_int(x, what) & 0xFF for x in v)
    s = re.sub(r"[\s:_-]|0x", "", str(v or ""))
    if len(s) % 2 or not re.fullmatch(r"[0-9a-fA-F]*", s):
        raise ParamError(f"{what}: not hex bytes: {v!r}")
    return bytes.fromhex(s)


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:60] or "x"


def stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def fmt_num(x: float, digits: int = 6) -> str:
    """Compact, locale-free number text for proposals."""
    if x == 0 or not math.isfinite(x):
        return repr(float(x)) if not math.isfinite(x) else "0"
    s = f"{x:.{digits}g}"
    return s


def deadline(seconds: float) -> Callable[[], float]:
    """Returns a function giving seconds left."""
    end = time.monotonic() + seconds
    return lambda: end - time.monotonic()


def env_path(name: str, default: str) -> str:
    return os.environ.get(name, default)
