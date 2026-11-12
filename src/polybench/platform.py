"""Every difference between operating systems lives here (UIR-56, W1-1).

Call sites import this module and never test `sys.platform` themselves. Today's macOS and Linux behaviour is unchanged; the
Windows branches are the new part. `psutil` is optional: with it, process work is exact; without it the module falls back to
`ps` (macOS, Linux) or `taskkill` and PowerShell (Windows), and says so in its return values or docstrings.

Safety (WIN-S1): `stop_tree` is what stops OpenOCD, esptool and anything they started. A child that was started with
`group_kwargs()` is stopped together with its whole tree on every OS.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

try:                                    # optional: exact process-tree and command-line work
    import psutil as _psutil
except ImportError:                     # pragma: no cover - exercised by the fallback tests through monkeypatch
    _psutil = None


def have_psutil() -> bool:
    return _psutil is not None


# ---------------------------------------------------------------------------------------------------- folders

def config_dir() -> Path:
    """Settings folder. macOS and Linux: ~/.config/polybench (unchanged). Windows: %APPDATA%\\polybench.
    POLYBENCH_CONFIG_DIR overrides it everywhere."""
    env = os.environ.get("POLYBENCH_CONFIG_DIR")
    if env:
        return Path(env).expanduser()
    if IS_WINDOWS:
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "polybench"
    return Path.home() / ".config" / "polybench"


def user_config_file(*parts: str, vendor: str = "polybench") -> Path:
    """A file in the per-user config area of this OS: ~/.config/<vendor>/<parts> on macOS and Linux, %APPDATA%\\<vendor>\\<parts> on Windows.
    Used for files other tools share (for example the instrument list of the older bench scripts)."""
    base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") if IS_WINDOWS else Path.home() / ".config"
    return base.joinpath(vendor, *parts)


def cache_dir() -> Path:
    """Disposable per-user state. macOS and Linux: ~/.cache/polybench (unchanged). Windows: %LOCALAPPDATA%\\polybench\\cache."""
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "polybench" / "cache"
    return Path.home() / ".cache" / "polybench"


def user_data_dir() -> Path:
    """Per-user application data (the demo project lives below it). macOS: ~/Library/Application Support/polybench.
    Linux: $XDG_DATA_HOME or ~/.local/share, then polybench (both unchanged). Windows: %LOCALAPPDATA%\\polybench."""
    home = Path.home()
    if IS_MACOS:
        return home / "Library" / "Application Support" / "polybench"
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "polybench"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base).expanduser() if base else home / ".local" / "share") / "polybench"


def kicad_prefs_dir() -> Path:
    """KiCad's own preferences folder (read only). KICAD_CONFIG_HOME-style overrides are handled by the caller."""
    if IS_WINDOWS:
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "kicad"
    if IS_MACOS:
        return Path.home() / "Library" / "Preferences" / "kicad"
    return Path.home() / ".config" / "kicad"


def is_within(path, root) -> bool:
    """True when `path` is `root` or inside it. Both should already be resolved. Compares whole path parts with this OS's rules
    (the case does not matter on Windows), so `/a/proj-sibling` is not inside `/a/proj` and `C:\\x\\p` is inside `c:\\X`."""
    p, r = os.path.normcase(os.path.abspath(str(path))), os.path.normcase(os.path.abspath(str(root)))
    if p == r:
        return True
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:                  # different drives
        return False


def temp_dir() -> Path:
    import tempfile
    return Path(tempfile.gettempdir())


# ---------------------------------------------------------------------------------------------------- files

def replace_file(src, dst, attempts: int = 8, wait: float = 0.05) -> None:
    """os.replace that survives Windows holding the target open for a moment (an editor, a virus scanner, the browser).
    On macOS and Linux it is exactly os.replace. On Windows a PermissionError is retried for about a second, then raised."""
    if not IS_WINDOWS:
        os.replace(src, dst)
        return
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(wait * (i + 1))


def show_in_file_manager(path: str) -> None:
    """Show a file or folder in Finder (`open -R`), Explorer (`explorer /select,`) or the default file manager on Linux
    (the containing folder). Returns at once; raises OSError when the helper cannot start."""
    if IS_MACOS:
        subprocess.Popen(["open", "-R", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif IS_WINDOWS:
        subprocess.Popen(["explorer", f"/select,{os.path.normpath(path)}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        target = path if os.path.isdir(path) else (os.path.dirname(path) or ".")
        subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------------------------------------------- serial ports

def is_usb_serial_call_out(device: str) -> bool:
    """False for macOS's `/dev/tty.*` twin of a `/dev/cu.*` port (only the call-out device is opened); True for everything else,
    including COM3 on Windows and /dev/ttyUSB0 on Linux."""
    return not device.startswith("/dev/tty.")


def list_serial_devices(pattern: str | None = None) -> list[str]:
    """Names of serial ports that can be opened, optionally filtered by a regular expression: `/dev/cu.*` on macOS,
    `/dev/ttyUSB*` and `/dev/ttyACM*` on Linux, `COMn` on Windows. Uses pyserial when installed, else the device folder."""
    import re
    rx = re.compile(pattern or ".")
    names: list[str] = []
    try:
        from serial.tools import list_ports
        names = [p.device for p in list_ports.comports() if is_usb_serial_call_out(p.device)]
    except ImportError:
        if not IS_WINDOWS:
            import glob
            names = glob.glob("/dev/cu.*") if IS_MACOS else glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")
    return sorted(n for n in names if rx.search(n))


def serial_port_hint() -> str:
    """An example port name for messages."""
    if IS_WINDOWS:
        return "COM3"
    return "/dev/cu.usbserial-0001" if IS_MACOS else "/dev/ttyUSB0"


def serial_port_openable(device: str) -> bool:
    """A cheap check before opening. Windows has no device permissions to test, so it is assumed openable until it is opened."""
    return True if IS_WINDOWS else os.access(device, os.R_OK | os.W_OK)


# ---------------------------------------------------------------------------------------------------- processes

def child_env() -> dict[str, str]:
    """Environment for a child Python process: UTF-8 text on every OS (Windows would use the legacy code page for pipes)."""
    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def group_kwargs() -> dict:
    """Popen/subprocess keyword arguments that put a child in its own process group (a Unix session, a Windows process group),
    so that `stop_tree` can reach it and everything it starts."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
    return {"start_new_session": True}


def detached_kwargs() -> dict:
    """Keyword arguments for a helper that must outlive this process (the AOI review server)."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}  # type: ignore[attr-defined]
    return {"start_new_session": True}


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if _psutil is not None:
        try:
            p = _psutil.Process(pid)
            return p.is_running() and p.status() != _psutil.STATUS_ZOMBIE
        except _psutil.NoSuchProcess:
            return False
        except _psutil.Error:
            return True
    if IS_WINDOWS:
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                                 timeout=5, encoding="utf-8", errors="replace").stdout
        except Exception:  # noqa: BLE001
            return False
        return f'"{int(pid)}"' in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # a zombie answers signal 0: ask ps for its state
    try:
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=3,
                            encoding="utf-8", errors="replace").stdout.strip()
    except Exception:  # noqa: BLE001
        return True
    return bool(st) and not st.startswith("Z")


def process_argv(pid: int) -> list[str] | None:
    """The words of a live process's command line, or None when it is gone or cannot be read (read only). With psutil the
    words are exact; without it the text of `ps` or PowerShell is split on spaces."""
    if _psutil is not None:
        try:
            return list(_psutil.Process(int(pid)).cmdline()) or None
        except _psutil.Error:
            return None
    text = process_command(pid)
    return text.split() if text else None


def process_command(pid: int) -> str | None:
    """Command line of a live process, or None when it is gone or cannot be read. Read only.
    psutil when installed; otherwise `ps -ww` (macOS, Linux; `-ww` lifts the 80-column cut of Linux procps) or PowerShell CIM (Windows)."""
    if _psutil is not None:
        try:
            parts = _psutil.Process(int(pid)).cmdline()
            return " ".join(parts) or None
        except _psutil.Error:
            return None
    try:
        if IS_WINDOWS:
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                                 capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace").stdout.strip()
        else:
            out = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(int(pid))], capture_output=True, text=True, timeout=4,
                                 encoding="utf-8", errors="replace").stdout.strip()
    except Exception:  # noqa: BLE001
        return None
    return out or None


def stop_hint(pid: int) -> str:
    """The command a person can type to stop and to check a process (for messages only; Polybench never runs it)."""
    if IS_WINDOWS:
        return f"taskkill /PID {pid} /T /F  (check first with: tasklist /FI \"PID eq {pid}\")."
    return f"kill {pid}  (check first with: ps -p {pid})."


def library_holders(lib_globs: list[str]) -> list[tuple[str, str]]:
    """[(pid, short command)] of processes that have one of these shared libraries loaded (used to find who holds the PCAN
    driver). macOS and Linux: `lsof`. Windows: not available, returns []; the caller then relies on the open attempt itself."""
    if IS_WINDOWS:
        return []
    import glob
    libs = sorted({os.path.realpath(x) for g in lib_globs for x in glob.glob(g)})
    if not libs:
        return []
    try:
        out = subprocess.run(["lsof", "-F", "pc", *libs], capture_output=True, text=True, timeout=4, encoding="utf-8", errors="replace").stdout
    except Exception:  # noqa: BLE001
        return []
    found: dict[str, str] = {}
    pid = None
    for ln in out.splitlines():
        if ln.startswith("p"):
            pid = ln[1:]
        elif ln.startswith("c") and pid and pid != str(os.getpid()):
            cmd = process_command(int(pid)) or ln[1:]
            toks = cmd.split()
            py = next((t for t in toks if t.endswith(".py")), None)
            found[pid] = (os.path.basename(py) + " " + " ".join(toks[toks.index(py) + 1:][:3])).strip() if py else cmd[:80]
    return sorted(found.items())


def _descendants(pid: int) -> list:
    if _psutil is None:
        return []
    try:
        return _psutil.Process(pid).children(recursive=True)
    except _psutil.Error:
        return []


def _group_signal(pid: int, sig) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def stop_tree(proc: subprocess.Popen, grace: float = 3.0) -> str:
    """Stop `proc` and every process it started, then return what it printed (empty when its output is not piped or was read).

    The child must have been started with `group_kwargs()`. macOS and Linux: SIGTERM to the group, wait `grace` seconds, then
    SIGKILL to the group. Windows: the tree is listed first (psutil), then `taskkill /T /F` is run on the root, then any listed
    process still alive is killed one by one, because Windows does not keep a process's children once their parent has gone.
    The tree is gone when this returns, unless the OS refused (then the survivors are named in the exception text of nothing:
    callers check `pid_alive`)."""
    out = ""
    if IS_WINDOWS:
        kids = _descendants(proc.pid)
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001
            pass
        for k in kids:
            try:
                k.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            out, _ = proc.communicate(timeout=max(grace, 3.0))
        except subprocess.TimeoutExpired:
            pass
        return out or ""
    kids = _descendants(proc.pid)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        _group_signal(proc.pid, sig)
        try:
            out, _ = proc.communicate(timeout=grace)
            break
        except subprocess.TimeoutExpired:
            continue
    for k in kids:                       # a grandchild that left the group (setsid) is still ours to stop
        try:
            if k.is_running():
                k.kill()
        except Exception:  # noqa: BLE001
            pass
    return out or ""


def kill_pid_tree(pid: int) -> None:
    """Hard-stop a process and its descendants by pid (used by tests and by cleanup of processes this program started).
    Never call it with a pid that Polybench did not start."""
    kids = _descendants(pid)
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001
            pass
    else:
        _group_signal(pid, signal.SIGKILL)
    for k in kids + ([_psutil.Process(pid)] if _psutil is not None and pid_alive(pid) else []):
        try:
            k.kill()
        except Exception:  # noqa: BLE001
            pass
    if not IS_WINDOWS and _psutil is None:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


# ---------------------------------------------------------------------------------------------------- KiCad link

def openocd_candidates() -> tuple[str, ...]:
    """Places to look for OpenOCD when it is not on PATH. macOS and Linux: Homebrew and /usr/local (unchanged).
    Windows: Program Files (an unverified guess: the bench run on Windows, W4, confirms it)."""
    if IS_WINDOWS:
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        return (os.path.join(pf, "OpenOCD", "bin", "openocd.exe"), r"C:\OpenOCD\bin\openocd.exe")
    return ("/opt/homebrew/bin/openocd", "/usr/local/bin/openocd")


def kicad_socket_candidates() -> list[str]:
    """Where a KiCad API socket may be. macOS and Linux: a Unix socket file under the temp folder (`/tmp/kicad/api.sock`).
    Windows: KiCad uses a named pipe, which is not a file path this module can probe; the real link is work package W3,
    so this returns []."""
    if IS_WINDOWS:
        return []
    return ["/tmp/kicad"]


def has_unix_sockets() -> bool:
    return not IS_WINDOWS
