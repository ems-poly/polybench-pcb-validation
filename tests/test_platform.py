"""polybench.platform (UIR-56, W1-1): one place for the OS differences. The rules are checked for every OS by switching the
module's flags, so the Windows branches are exercised on macOS and Linux too (the Windows process work itself runs for real
in tests/test_platform_tree_stop.py on a Windows runner)."""
import sys
from pathlib import Path

import pytest

from polybench import platform as pf


@pytest.fixture
def as_os(monkeypatch):
    def pick(name):
        monkeypatch.setattr(pf, "IS_WINDOWS", name == "windows")
        monkeypatch.setattr(pf, "IS_MACOS", name == "macos")
        monkeypatch.setattr(pf, "IS_LINUX", name == "linux")
    for k in ("POLYBENCH_CONFIG_DIR", "XDG_DATA_HOME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("APPDATA", r"C:\Users\x\AppData\Roaming")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    return pick


def test_macos_and_linux_paths_are_the_ones_of_today(as_os):
    home = Path.home()
    as_os("macos")
    assert pf.config_dir() == home / ".config" / "polybench"
    assert pf.cache_dir() == home / ".cache" / "polybench"
    assert pf.user_data_dir() == home / "Library" / "Application Support" / "polybench"
    assert pf.kicad_prefs_dir() == home / "Library" / "Preferences" / "kicad"
    assert pf.user_config_file("instruments.toml", vendor="polystage-bench") == home / ".config" / "polystage-bench" / "instruments.toml"
    as_os("linux")
    assert pf.user_data_dir() == home / ".local" / "share" / "polybench"
    assert pf.config_dir() == home / ".config" / "polybench"


def test_xdg_data_home_is_honoured_on_linux_only(as_os, monkeypatch):
    as_os("linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/data/x")
    assert pf.user_data_dir() == Path("/data/x") / "polybench"


def test_windows_paths_use_appdata(as_os):
    as_os("windows")
    assert str(pf.config_dir()).endswith("Roaming\\polybench") or str(pf.config_dir()).endswith("Roaming/polybench")
    assert pf.cache_dir().parts[-2:] == ("polybench", "cache")
    assert pf.user_data_dir().name == "polybench" and "Local" in str(pf.user_data_dir())
    assert pf.kicad_prefs_dir().name == "kicad"


def test_config_dir_env_override_wins_everywhere(as_os, monkeypatch, tmp_path):
    monkeypatch.setenv("POLYBENCH_CONFIG_DIR", str(tmp_path))
    for name in ("macos", "linux", "windows"):
        as_os(name)
        assert pf.config_dir() == tmp_path


def test_process_group_arguments(as_os):
    as_os("macos")
    assert pf.group_kwargs() == {"start_new_session": True}
    as_os("linux")
    assert pf.detached_kwargs() == {"start_new_session": True}
    if sys.platform == "win32":
        as_os("windows")
        assert "creationflags" in pf.group_kwargs()


def test_serial_names(as_os, monkeypatch):
    assert pf.is_usb_serial_call_out("COM3") and pf.is_usb_serial_call_out("/dev/cu.usbserial-1") and pf.is_usb_serial_call_out("/dev/ttyUSB0")
    assert not pf.is_usb_serial_call_out("/dev/tty.usbserial-1")
    as_os("windows")
    assert pf.serial_port_hint() == "COM3"
    assert pf.serial_port_openable("COM3")
    as_os("macos")
    assert pf.serial_port_hint().startswith("/dev/cu.")


def test_child_env_forces_utf8():
    e = pf.child_env()
    assert e["PYTHONUTF8"] == "1" and e["PYTHONIOENCODING"] == "utf-8"


def test_process_commands_are_read_with_and_without_psutil(monkeypatch):
    import os
    me = os.getpid()
    with_psutil = pf.process_argv(me)
    assert with_psutil and any("python" in w.lower() or "pytest" in w.lower() for w in with_psutil)
    monkeypatch.setattr(pf, "_psutil", None)
    text = pf.process_command(me)                          # `ps` or PowerShell
    assert text and ("python" in text.lower() or "pytest" in text.lower())
    assert pf.process_command(2 ** 22 + 12345) is None


def test_pid_alive_with_and_without_psutil(monkeypatch):
    import os
    import subprocess
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert pf.pid_alive(p.pid) and pf.pid_alive(os.getpid())
        monkeypatch.setattr(pf, "_psutil", None)
        assert pf.pid_alive(p.pid)
    finally:
        p.kill()
        p.wait()
    assert not pf.pid_alive(p.pid)
    monkeypatch.setattr(pf, "_psutil", None)
    assert not pf.pid_alive(p.pid)
    assert not pf.pid_alive(0)


def test_replace_file_replaces_and_retries_a_held_target_on_windows(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("new", encoding="utf-8")
    b.write_text("old", encoding="utf-8")
    pf.replace_file(a, b)
    assert b.read_text(encoding="utf-8") == "new" and not a.exists()
    # Windows: a PermissionError for the first two tries (an editor holds the file), then it works
    monkeypatch.setattr(pf, "IS_WINDOWS", True)
    monkeypatch.setattr(pf.time, "sleep", lambda s: None)
    calls = []
    real = pf.os.replace

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("held")
        real(src, dst)
    monkeypatch.setattr(pf.os, "replace", flaky)
    a.write_text("newer", encoding="utf-8")
    pf.replace_file(a, b)
    assert b.read_text(encoding="utf-8") == "newer" and len(calls) == 3
    # and it gives up with the real error when the file stays held
    monkeypatch.setattr(pf.os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError("held")))
    a.write_text("x", encoding="utf-8")
    with pytest.raises(PermissionError):
        pf.replace_file(a, b)


def test_show_in_file_manager_picks_the_right_helper(as_os, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(pf.subprocess, "Popen", lambda cmd, **kw: seen.append(cmd))
    f = tmp_path / "x.txt"
    f.write_text("x", encoding="utf-8")
    as_os("macos")
    pf.show_in_file_manager(str(f))
    as_os("windows")
    pf.show_in_file_manager(str(f))
    as_os("linux")
    pf.show_in_file_manager(str(f))
    assert seen[0][:2] == ["open", "-R"]
    assert seen[1][0] == "explorer" and seen[1][1].startswith("/select,")
    assert seen[2] == ["xdg-open", str(tmp_path)]


def test_kicad_socket_candidates_are_empty_on_windows_until_w3(as_os):
    as_os("windows")
    assert pf.kicad_socket_candidates() == [] and not pf.has_unix_sockets()
    as_os("macos")
    assert pf.kicad_socket_candidates() == ["/tmp/kicad"]


def test_shell_quote_and_restore_command_for_each_os(as_os):
    as_os("macos")
    assert pf.shell_quote("/a b/PLAN.md") == "'/a b/PLAN.md'" and pf.shell_quote("/a/PLAN.md") == "/a/PLAN.md"
    assert pf.restore_file_command("/a/P.md", "/h/1.md") == "{ [ ! -e /a/P.md ] || cp -p /a/P.md /a/P.md.broken; } && cp -p /h/1.md /a/P.md"
    as_os("windows")
    assert pf.shell_quote(r"C:\a b\P.md") == '"C:\\a b\\P.md"'
    cmd = pf.restore_file_command(r"C:\a b\P.md", r"C:\h\1.md")
    # the backup and the copy are chained with &&, never &: a failed backup must not overwrite the broken master
    assert cmd == ('if exist "C:\\a b\\P.md" (copy /Y "C:\\a b\\P.md" "C:\\a b\\P.md.broken" && copy /Y "C:\\h\\1.md" "C:\\a b\\P.md") '
                   'else (copy /Y "C:\\h\\1.md" "C:\\a b\\P.md")')
    assert " & " not in cmd
    # a % in a path cannot be quoted safely for cmd: no command is made, the caller explains
    assert pf.restore_file_command(r"C:\a\100%.md", r"C:\h\1.md") == "" and pf.restore_file_command(r"C:\a\P.md", r"C:\h\%PATH%.md") == ""
    as_os("linux")
    assert pf.restore_file_command("/a/100%.md", "/h/1.md").endswith("cp -p /h/1.md /a/100%.md")        # sh has no such problem


def test_recovery_text_says_what_to_do_when_the_windows_path_has_a_percent(as_os, monkeypatch, tmp_path):
    from polybench.ai import recovery
    as_os("windows")
    info = {"master": "C:\\a\\100%.md", "restored_from": "C:\\h\\1.md", "error": "x",
            "command": pf.restore_file_command("C:\\a\\100%.md", "C:\\h\\1.md"),
            "command_note": "the path contains a % sign, which cmd.exe cannot take safely in a command: copy it by hand"}
    txt = recovery.console_text(info, True)
    assert "copy it by hand" in txt and "  \n" not in txt


def test_is_within_compares_path_parts_not_text(as_os, tmp_path):
    assert pf.is_within(tmp_path / "proj" / "x", tmp_path / "proj") and pf.is_within(tmp_path / "proj", tmp_path / "proj")
    assert not pf.is_within(str(tmp_path / "proj") + "-sibling", tmp_path / "proj")
    assert not pf.is_within(tmp_path, tmp_path / "proj")


def test_voice_and_trial_files_stay_in_the_default_folder_when_the_config_env_is_set(monkeypatch, tmp_path):
    """D7: POLYBENCH_CONFIG_DIR moves the project list only, as on main; voice.json and claude-mcp-trial.json stay where they were."""
    from polybench.ai import cli
    from polybench.voice import config as vc
    monkeypatch.delenv("POLYBENCH_VOICE_CONFIG", raising=False)
    monkeypatch.setenv("POLYBENCH_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert pf.config_dir() == tmp_path / "elsewhere"
    assert pf.default_config_dir() != tmp_path / "elsewhere"
    assert vc.path() == pf.default_config_dir() / "voice.json"
    import inspect
    assert "default_config_dir()" in inspect.getsource(cli.write_trial_config) or "default_config_dir()" in inspect.getsource(cli)


def test_an_empty_config_env_means_unset(monkeypatch):
    monkeypatch.setenv("POLYBENCH_CONFIG_DIR", "")
    assert pf.config_dir() == pf.default_config_dir()
