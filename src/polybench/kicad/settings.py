"""Where KiCad's API socket would be, and what KiCad's own settings file says about the API server. Everything here is read only.

TBR-11: the exact label of the switch in KiCad 10's Preferences is not confirmed; the text below names the setting key that
is certain (`api.enable_server`) and the menu path reported by third parties.
"""
from __future__ import annotations

import glob
import json
import os
import tempfile
from pathlib import Path

ENABLE_HINT = ("enable the KiCad API server (KiCad, Preferences, Plugins, 'Enable KiCad API server'; the setting is "
               "api.enable_server in kicad_common.json) and restart KiCad")
NOT_RUNNING_DETAIL = "KiCad is not running or its API server is off"
API_OFF_DETAIL = "KiCad's API server is off (api.enable_server is false in kicad_common.json): " + ENABLE_HINT


def prefs_dir() -> Path:
    env = os.environ.get("POLYBENCH_KICAD_PREFS")
    return Path(env) if env else Path.home() / "Library" / "Preferences" / "kicad"


def common_json_paths() -> list[Path]:
    """kicad_common.json of every KiCad version found, newest first."""
    base = prefs_dir()
    if (base / "kicad_common.json").exists():          # POLYBENCH_KICAD_PREFS may point straight at a version folder
        return [base / "kicad_common.json"]
    try:
        vers = sorted((p for p in base.iterdir() if (p / "kicad_common.json").exists()),
                      key=lambda p: [int(x) if x.isdigit() else 0 for x in p.name.split(".")], reverse=True)
    except OSError:
        return []
    return [p / "kicad_common.json" for p in vers]


def api_enabled() -> tuple[bool | None, str]:
    """(enabled, where). None when the settings file cannot be read or has no api section. The file is opened for reading only."""
    for p in common_json_paths():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        api = data.get("api")
        if isinstance(api, dict) and "enable_server" in api:
            return bool(api["enable_server"]), str(p)
    return None, ""


def socket_dirs() -> list[str]:
    dirs = ["/tmp/kicad"]
    t = os.path.join(tempfile.gettempdir(), "kicad")        # KiCad documents "a kicad folder of the temp directory"
    if t not in dirs:
        dirs.append(t)
    return dirs


def socket_files() -> list[str]:
    """Existing API socket files: KICAD_API_SOCKET if it names one, else api.sock and api-<pid>.sock (TBR-3: the second-instance
    name is unverified) in the socket directories. Order: the plain api.sock first."""
    env = os.environ.get("KICAD_API_SOCKET") or os.environ.get("POLYBENCH_KICAD_SOCKET")
    if env:
        return [env]            # a non-ipc address is kept so that the link refuses it by name (KC-35)
    found: list[str] = []
    for d in socket_dirs():
        plain = os.path.join(d, "api.sock")
        if os.path.exists(plain) and plain not in found:
            found.append(plain)
        for p in sorted(glob.glob(os.path.join(d, "api*.sock"))):
            if p not in found:
                found.append(p)
    return found


def check_scheme(address: str) -> str:
    """KC-35: only a local ipc:// path (or a bare path) is accepted. Returns the ipc:// address. Raises ValueError otherwise."""
    if "://" in address:
        if not address.startswith("ipc://"):
            raise ValueError(f"refused: only a local ipc:// socket is allowed, got {address.split('://')[0]}://")
        return address
    return "ipc://" + address
