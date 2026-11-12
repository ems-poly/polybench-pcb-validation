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
from dataclasses import dataclass, field
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

def default_config_dir() -> Path:
    """The per-user settings folder, ignoring POLYBENCH_CONFIG_DIR. macOS and Linux: ~/.config/polybench (unchanged).
    Windows: %APPDATA%\\polybench. voice.json and claude-mcp-trial.json live here, where they always did."""
    if IS_WINDOWS:
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "polybench"
    return Path.home() / ".config" / "polybench"


def config_dir() -> Path:
    """The project-list folder (workspace.py): POLYBENCH_CONFIG_DIR when it is set and not empty, else `default_config_dir()`.
    (An empty value used to mean the current folder; it now means unset.)"""
    env = os.environ.get("POLYBENCH_CONFIG_DIR")
    return Path(env).expanduser() if env else default_config_dir()


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


def shell_quote(text: str) -> str:
    """Quote one word for a command line a person will paste: POSIX shell rules on macOS and Linux (shlex.quote, unchanged),
    double quotes on Windows. For text shown to the operator only; Polybench never runs it through a shell itself."""
    if IS_WINDOWS:
        return '"' + str(text).replace('"', '\\"') + '"'
    import shlex
    return shlex.quote(str(text))


def restore_file_command(master: str, good: str) -> str:
    """A one-line command that keeps a broken `master` as `master.broken` (when it exists) and copies `good` over it.
    macOS and Linux: sh, unchanged. Windows: cmd.exe, HOST-ONLY text (checked as text, never run on a bench). The backup and the
    copy are joined with `&&` inside `if exist ... (...) else (...)`, so a failed backup stops the command and the broken master is
    not overwritten. A path with `%` cannot be quoted safely at a cmd prompt (`%NAME%` expands even inside quotes, and `%%` is
    only for batch files), so on Windows this returns "" and the caller tells the person to copy the file by hand."""
    q = shell_quote
    if IS_WINDOWS:
        if "%" in master or "%" in good:
            return ""
        return (f'if exist {q(master)} (copy /Y {q(master)} {q(master + ".broken")} && copy /Y {q(good)} {q(master)}) '
                f'else (copy /Y {q(good)} {q(master)})')
    return f"{{ [ ! -e {q(master)} ] || cp -p {q(master)} {q(master + '.broken')}; }} && cp -p {q(good)} {q(master)}"


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
            out = ""
            for shell in ("powershell", "pwsh"):               # Windows PowerShell first, PowerShell 7 if that is all there is
                try:
                    out = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command",
                                          f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                                         capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace").stdout.strip()
                except Exception:  # noqa: BLE001
                    continue
                if out:
                    break
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


@dataclass
class StopResult:
    """What `stop_tree` found. `survivors` is [(pid, command)] of processes still alive after every step: the caller must tell the
    operator (never report a clean stop). `output` is what the tool printed."""
    output: str = ""
    survivors: list = field(default_factory=list)

    @property
    def stopped(self) -> bool:
        return not self.survivors

    def __str__(self) -> str:                    # old callers used the text
        return self.output


def survivor_message(tool: str, res: StopResult, why: str = "was asked to stop") -> str:
    """The operator-facing text when `stop_tree` left something running. Names every pid, like the OpenOCD survivor report."""
    who = "; ".join(f"pid {pid} ('{(cmd or '?')[:80]}')" for pid, cmd in res.survivors)
    hints = " ".join(stop_hint(pid) for pid, _ in res.survivors[:3])
    return (f"{tool} {why}, but it may still be running: {who}. It can hold the probe or the serial port. Polybench could not stop it "
            f"and will not claim a clean stop. If you are sure nothing is programming, stop it yourself: {hints}")


def _descendants(pid: int) -> list:
    if _psutil is None:
        return []
    try:
        return _psutil.Process(pid).children(recursive=True)
    except _psutil.Error:
        return []


def _tree_pids(pid: int) -> set[int]:
    """Pids of everything below `pid` (not `pid` itself). psutil when installed; on Windows without it, a PowerShell
    snapshot of the process table; on macOS and Linux without psutil the process group is the list (see _group_members)."""
    if _psutil is not None:
        return {k.pid for k in _descendants(pid)}
    if not IS_WINDOWS:
        return set()
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                              "Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId) $($_.ParentProcessId)\" }"],
                             capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace").stdout
    except Exception:  # noqa: BLE001
        return set()
    parent: dict[int, int] = {}
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            parent[int(parts[0])] = int(parts[1])
    found: set[int] = set()
    todo = [pid]
    while todo:
        cur = todo.pop()
        for c, par in parent.items():
            if par == cur and c not in found and c != pid:
                found.add(c)
                todo.append(c)
    return found


def _group_signal(pid: int, sig) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _group_members(pgid: int) -> list[tuple[int, str]]:
    """Live (not zombie) members of a Unix process group: [(pid, command)]. Empty when the group is gone."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return []
    except PermissionError:
        pass
    found: list[tuple[int, str]] = []
    if _psutil is not None:
        for pr in _psutil.process_iter(["pid", "status", "cmdline"]):
            try:
                if pr.info["status"] != _psutil.STATUS_ZOMBIE and os.getpgid(pr.info["pid"]) == pgid:
                    found.append((pr.info["pid"], " ".join(pr.info["cmdline"] or [])))
            except (_psutil.Error, OSError):
                continue
        return found
    try:
        out = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,pgid=,stat=,command="], capture_output=True, text=True, timeout=5,
                             encoding="utf-8", errors="replace").stdout
    except Exception:  # noqa: BLE001
        return [(pgid, "process group")]                 # killpg says something is there and ps cannot say what
    for ln in out.splitlines():
        parts = ln.split(None, 3)
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit() and int(parts[1]) == pgid and not parts[2].startswith("Z"):
            found.append((int(parts[0]), parts[3] if len(parts) > 3 else ""))
    return found


def _wait_gone(proc: subprocess.Popen, alive, seconds: float) -> list:
    """Poll `alive()` (a list of survivors) every 50 ms for up to `seconds`; reaps `proc` meanwhile so it is not a zombie."""
    end = time.monotonic() + seconds
    while True:
        proc.poll()
        now = alive()
        if not now or time.monotonic() >= end:
            return now
        time.sleep(0.05)


def _drain(proc: subprocess.Popen, seconds: float) -> str:
    try:
        out, _ = proc.communicate(timeout=seconds)
        return out or ""
    except subprocess.TimeoutExpired:                    # a survivor still holds the pipe: take what we can and move on
        return ""
    except (ValueError, OSError):
        return ""


def stop_tree(proc: subprocess.Popen, grace: float = 3.0) -> StopResult:
    """Stop `proc` and everything it started, and say whether anything is still alive.

    The child must have been started with `group_kwargs()`.
    macOS and Linux: SIGTERM to the process group; wait up to `grace` seconds for the WHOLE group (not just the root) to be gone;
      SIGKILL to the group and to every psutil-listed descendant; wait again (up to 3 s); report what is left.
    Windows: list the tree first; CTRL_BREAK_EVENT to the process group (graceful); wait up to `grace` seconds for every listed
      process; then `taskkill /T /F` on the root and `taskkill /F` on each survivor; wait again; report what is left.
    Returns a StopResult. `stopped` is True only when nothing of the tree is alive: callers must not report a clean cancel or
    timeout otherwise (see `survivor_message`)."""
    if IS_WINDOWS:
        return _stop_tree_windows(proc, grace)
    pgid = proc.pid
    kids = _descendants(proc.pid)                       # psutil only: a descendant that left the group (setsid) is still ours

    def alive() -> list:
        out = _group_members(pgid)
        seen = {pid for pid, _ in out}
        for k in kids:
            try:
                if k.pid not in seen and k.is_running() and k.status() != _psutil.STATUS_ZOMBIE:
                    out.append((k.pid, " ".join(k.cmdline())))
            except Exception:  # noqa: BLE001
                continue
        return out

    _group_signal(pgid, signal.SIGTERM)
    for k in kids:
        try:
            k.terminate()
        except Exception:  # noqa: BLE001
            pass
    left = _wait_gone(proc, alive, grace)
    if left:
        _group_signal(pgid, signal.SIGKILL)
        for k in kids:
            try:
                k.kill()
            except Exception:  # noqa: BLE001
                pass
        left = _wait_gone(proc, alive, 3.0)
    return StopResult(_drain(proc, 1.0 if left else 3.0), left)


def _stop_tree_windows(proc: subprocess.Popen, grace: float) -> StopResult:
    def alive_of(pids) -> list:
        return [(x, process_command(x) or "") for x in sorted(pids) if pid_alive(x)]

    tree = {proc.pid} | _tree_pids(proc.pid)
    try:
        proc.send_signal(signal.CTRL_BREAK_EVENT)            # graceful: reaches a process group made with CREATE_NEW_PROCESS_GROUP
    except (OSError, ValueError, AttributeError):
        pass
    left = _wait_gone(proc, lambda: [(x, "") for x in sorted(tree) if pid_alive(x)], grace)
    if left:
        tree |= _tree_pids(proc.pid)                           # anything started since the first look
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=15)
        except Exception:  # noqa: BLE001
            pass
        for x in sorted(tree):
            if pid_alive(x):
                try:
                    subprocess.run(["taskkill", "/PID", str(x), "/F"], capture_output=True, timeout=15)
                except Exception:  # noqa: BLE001
                    pass
                if _psutil is not None and pid_alive(x):
                    try:
                        _psutil.Process(x).kill()
                    except Exception:  # noqa: BLE001
                        pass
        left = _wait_gone(proc, lambda: [(x, "") for x in sorted(tree) if pid_alive(x)], 3.0)
        left = alive_of({x for x, _ in left})
    else:
        left = []
    return StopResult(_drain(proc, 1.0 if left else 3.0), left)


def kill_pid_tree(pid: int) -> None:
    """Hard-stop a process and its descendants by pid (used by tests to clean up, and for processes this program started).
    Never call it with a pid that Polybench did not start."""
    kids = _descendants(pid)
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001
            pass
    else:
        _group_signal(pid, signal.SIGKILL)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    for k in kids:
        try:
            k.kill()
        except Exception:  # noqa: BLE001
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
