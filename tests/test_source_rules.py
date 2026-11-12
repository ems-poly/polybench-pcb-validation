"""Source rules for the Windows port (UIR-56): checked by reading the code, on every OS.

W1-1  `src/` tests the operating system in one place only: polybench/platform.py.
W1-3  every text file Polybench reads or writes names its encoding (Windows would otherwise use the legacy code page, and a
      reading such as "10 kOhm" with the Greek capital omega would crash the action log)."""
import ast
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


OS_ONLY = {("sys", "platform"), ("os", "name"), ("os", "killpg"), ("os", "getpgid"), ("os", "setsid"), ("os", "geteuid"),
           ("signal", "SIGKILL"), ("platform", "system")}


def test_only_platform_py_tests_the_operating_system():
    bad = []
    for p in FILES:
        if p.name == "platform.py" and p.parent == SRC:
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and (node.value.id, node.attr) in OS_ONLY:
                bad.append(f"{p.relative_to(SRC)}:{node.lineno}: {node.value.id}.{node.attr}")
            if isinstance(node, ast.keyword) and node.arg == "start_new_session":
                bad.append(f"{p.relative_to(SRC)}:{node.value.lineno}: start_new_session")
    assert not bad, "use polybench.platform (W1-1):\n" + "\n".join(bad)
