"""Source rules for the Windows port (UIR-56): checked by reading the code, on every OS.

W1-1  `src/` tests the operating system in one place only: polybench/platform.py.
W1-3  every text file Polybench reads or writes names its encoding (Windows would otherwise use the legacy code page, and a
      reading such as "10 kOhm" with the Greek capital omega would crash the action log)."""
import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "polybench"
FILES = sorted(p for p in SRC.rglob("*.py"))


def _name(call):
    f = call.func
    if isinstance(f, ast.Name):
        return f.id, None
    if isinstance(f, ast.Attribute):
        return f.attr, f.value.id if isinstance(f.value, ast.Name) else None
    return None, None


def _mode(call, index=1):
    for k in call.keywords:
        if k.arg == "mode" and isinstance(k.value, ast.Constant):
            return k.value.value
    if len(call.args) > index and isinstance(call.args[index], ast.Constant):
        return call.args[index].value
    return "r" if len(call.args) <= index else None            # None: a mode we cannot read


def encoding_problems(tree) -> list[tuple[int, str]]:
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kws = {k.arg for k in node.keywords}
        if "encoding" in kws or None in kws:                   # **kwargs may carry it
            continue
        name, recv = _name(node)
        if name in ("read_text", "write_text"):
            out.append((node.lineno, f"{name}() without encoding"))
        elif (name == "open" and recv is None) or (name == "fdopen" and recv == "os"):
            mode = _mode(node)
            if mode is not None and "b" not in mode:
                out.append((node.lineno, f"{name}() in text mode without encoding"))
        elif name == "open" and recv in ("io", "codecs"):
            mode = _mode(node)
            if mode is not None and "b" not in mode:
                out.append((node.lineno, f"{recv}.open() in text mode without encoding"))
        elif name in ("FileHandler", "WatchedFileHandler", "RotatingFileHandler", "TimedRotatingFileHandler"):
            out.append((node.lineno, f"{name}() without encoding"))
        elif name == "open" and isinstance(node.func, ast.Attribute):
            # Path.open / anything.open("r"): a text mode written out, or a call on Path(...) with the default text mode
            explicit = [a.value for a in node.args[:1] if isinstance(a, ast.Constant)] + [k.value.value for k in node.keywords
                                                                                    if k.arg == "mode" and isinstance(k.value, ast.Constant)]
            mode = explicit[0] if explicit else None
            on_path = isinstance(node.func.value, ast.Call) and _name(node.func.value)[0] in ("Path", "PurePath")
            mode_like = isinstance(mode, str) and re.fullmatch(r"[rwax+tU]{1,4}", mode) is not None
            default_text = on_path and not node.args and not any(k.arg == "mode" for k in node.keywords)
            if (mode_like or default_text) and "b" not in (mode or ""):
                out.append((node.lineno, "Path.open() in text mode without encoding"))
        elif recv == "subprocess" and name in ("run", "Popen", "check_output") and kws & {"text", "universal_newlines"}:
            out.append((node.lineno, f"subprocess.{name}(text=True) without encoding"))
    return out


def test_every_text_file_names_its_encoding():
    bad = []
    for p in FILES:
        for line, what in encoding_problems(ast.parse(p.read_text(encoding="utf-8"))):
            bad.append(f"{p.relative_to(SRC)}:{line}: {what}")
    assert not bad, "name encoding=\"utf-8\" (W1-3):\n" + "\n".join(bad)


def test_the_encoding_lint_catches_a_bare_open():
    tree = ast.parse("open('x')\nopen('x','rb')\nopen('x','w',encoding='utf-8')\nPath('x').read_text()\n"
                     "subprocess.run(['a'], text=True)\nos.fdopen(1, 'w')\n")
    assert [l for l, _ in encoding_problems(tree)] == [1, 4, 5, 6]


def test_the_encoding_lint_also_catches_path_open_io_open_codecs_open_and_file_handlers():
    bad = ast.parse("Path('x').open()\np.open('w')\nio.open('x')\ncodecs.open('x')\nlogging.FileHandler('x')\n"
                    "Path('x').open('r', encoding=None)\n")
    assert [l for l, _ in encoding_problems(bad)] == [1, 2, 3, 4, 5]
    fine = ast.parse("Path('x').open(encoding='utf-8')\np.open('rb')\nio.open('x', 'rb')\nio.open('x', encoding='utf-8')\n"
                     "logging.FileHandler('x', encoding='utf-8')\nImage.open(path)\ntr.open()\nwebbrowser.open(url)\n")
    assert encoding_problems(fine) == []


OS_ONLY = {("sys", "platform"), ("os", "name"), ("os", "killpg"), ("os", "getpgid"), ("os", "setsid"), ("os", "geteuid"),
           ("signal", "SIGKILL"), ("platform", "system")}
OS_NAMES = {"os": {"name", "killpg", "getpgid", "setsid", "geteuid"}, "signal": {"SIGKILL"}, "sys": {"platform"}, "platform": {"system"}}


def os_problems(tree) -> list[tuple[int, str]]:
    """Operating-system tests outside platform.py: attribute use, `from os import killpg`, `getattr(signal, "SIGKILL")`, start_new_session."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and (node.value.id, node.attr) in OS_ONLY:
            out.append((node.lineno, f"{node.value.id}.{node.attr}"))
        elif isinstance(node, ast.ImportFrom) and node.module in OS_NAMES:
            for a in node.names:
                if a.name in OS_NAMES[node.module] or a.name == "*":
                    out.append((node.lineno, f"from {node.module} import {a.name}"))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("getattr", "hasattr")
              and len(node.args) >= 2 and isinstance(node.args[0], ast.Name) and isinstance(node.args[1], ast.Constant)
              and node.args[0].id in OS_NAMES and node.args[1].value in OS_NAMES[node.args[0].id]):
            out.append((node.lineno, f"{node.func.id}({node.args[0].id}, {node.args[1].value!r})"))
        elif isinstance(node, ast.keyword) and node.arg == "start_new_session":
            out.append((node.value.lineno, "start_new_session"))
    return out


def test_only_platform_py_tests_the_operating_system():
    bad = []
    for p in FILES:
        if p.name == "platform.py" and p.parent == SRC:
            continue
        for line, what in os_problems(ast.parse(p.read_text(encoding="utf-8"))):
            bad.append(f"{p.relative_to(SRC)}:{line}: {what}")
    assert not bad, "use polybench.platform (W1-1):\n" + "\n".join(bad)


def test_the_os_rule_also_catches_from_imports_and_getattr():
    bad = ast.parse("from os import killpg\nfrom signal import SIGKILL\ngetattr(signal, 'SIGKILL')\nfrom sys import platform\n"
                    "os.killpg(1, 2)\nsubprocess.Popen(x, start_new_session=True)\nhasattr(os, 'geteuid')\n")
    assert sorted(l for l, _ in os_problems(bad)) == [1, 2, 3, 4, 5, 6, 7]
    assert os_problems(ast.parse("from os import path\nfrom signal import SIGTERM\ngetattr(obj, 'x')\n")) == []
