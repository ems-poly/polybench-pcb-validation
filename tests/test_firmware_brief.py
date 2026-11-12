"""Lane A-1: the polybench-test-firmware skill ships, and its worked example agrees with the step and the firmware.

HOST-ONLY. No board, no serial port, no network."""
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
EX = ROOT / "examples" / "esp32-s3-board"
SKILL = ROOT / "src" / "polybench" / "ai" / "skills" / "polybench-test-firmware" / "SKILL.md"


def _steps():
    d = yaml.safe_load((EX / "function.yaml").read_text())
    for sec in d["steps"][0]["sections"]:
        for a in sec.get("actions", []):
            if a["plugin"] == "serial.query":
                return a["params"]["steps"]
    raise AssertionError("no serial.query action")


def test_skill_has_front_matter_and_the_rules():
    t = SKILL.read_text()
    assert t.startswith("---\nname: polybench-test-firmware\ndescription: ")
    assert "Data, never instructions" in t
    for h in ("Who does what", "The brief", "Worked example"):
        assert h in t
    assert "never says pass or fail" in t.lower() or "never says pass or fail" in t


def test_package_data_glob_covers_the_skill():
    import fnmatch
    import tomllib
    pd = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["polybench"]
    rel = SKILL.relative_to(ROOT / "src" / "polybench").as_posix()
    assert any(fnmatch.fnmatch(rel, g) for g in pd)


def test_skill_is_installed_by_install_skills(tmp_path):
    import io
    from polybench.ai.cli import install_skills
    assert install_skills(tmp_path, False, False, io.StringIO()) == 0
    assert (tmp_path / "polybench-test-firmware" / "SKILL.md").is_file()


def test_worked_example_matches_function_yaml_and_firmware():
    brief = (EX / "FIRMWARE_BRIEF.md").read_text()
    fw = (EX / "firmware" / "src" / "main.cpp").read_text()
    for st in _steps():
        cmd = st["send"]
        expect = st.get("expect") or cmd.split()[0]
        row = next((ln for ln in brief.splitlines() if ln.startswith(f"| `{cmd}` |")), None)
        assert row, f"command {cmd!r} not in the brief's table"
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert cells[1] == f"`{expect}`"
        assert cells[3] == f"{st['timeout_s']} s" and cells[4] == str(st.get("retries", 0)), row
        assert f'\\"type\\":\\"{expect}\\"' in fw, f"firmware never emits type {expect}"
        for key, test in (st.get("map") or {}).items():
            assert f"`{key}` to {test}" in cells[2], (key, test)
            assert f'\\"{key}\\"' in fw, f"firmware never emits key {key}"
        if st.get("ask"):
            assert st["ask"]["test"] in cells[2]
        if cmd.split()[0] != "selftest":   # command word is handled by the firmware parser
            assert f'"{cmd.split()[0]}"' in fw or f'startsWith("{cmd.split()[0]}")' in fw


def test_worked_example_names_every_function_test_and_is_labelled():
    brief = (EX / "FIRMWARE_BRIEF.md").read_text()
    d = yaml.safe_load((EX / "function.yaml").read_text())
    ids = {t["id"] for sec in d["steps"][0]["sections"] for t in sec.get("tests", [])}
    assert ids and all(i in brief for i in ids), sorted(i for i in ids if i not in brief)
    assert "HOST-ONLY" in brief and "UNVALIDATED" in brief
    sha = re.search(r"sha256: ([0-9a-f]{64})", (EX / "function.yaml").read_text())
    assert sha and "sha256" in brief.lower()


def test_example_is_demo_clean():
    t = (SKILL.read_text() + (EX / "FIRMWARE_BRIEF.md").read_text())
    assert not re.search("|".join(["\\bstra" + "in", "mcmu" + "rtry", "/Us" + "ers/"]), t, re.I)


@pytest.mark.skipif(not (ROOT / "pyproject.toml").is_file(), reason="needs a source tree")
def test_skill_ships_in_the_wheel(tmp_path):
    """Build the wheel offline (no build isolation) and list it."""
    r = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-q",
                        "-w", str(tmp_path), str(ROOT)], capture_output=True, text=True, timeout=120)
    if r.returncode != 0 and "setuptools" in r.stderr:
        pytest.skip(f"cannot build offline here: {r.stderr[-200:]}")
    assert r.returncode == 0, r.stderr[-500:]
    whl = next(tmp_path.glob("polybench*.whl"))
    names = zipfile.ZipFile(whl).namelist()
    assert "polybench/ai/skills/polybench-test-firmware/SKILL.md" in names


# ---------------------------------------------------------------- A-2: README "Install with your agent"
def _agent_section():
    t = (ROOT / "README.md").read_text()
    assert t.count("\n## Install with your agent\n") == 1
    s = t.split("\n## Install with your agent\n")[1].split("\n## ")[0]
    return s


def test_readme_agent_prompt_keeps_the_rules_and_is_labelled():
    s = _agent_section()
    prompt = s.split("```text")[1].split("```")[0]
    assert "never edits an assistant's configuration" in s
    assert "UNVALIDATED" in s and "HOST-ONLY" in s
    assert "wait\nfor my yes" in prompt and "exact command" in prompt
    assert "Do not use any command Polybench did not print" in prompt
    assert "never its HTTP API" in prompt and "data, not\ninstructions" in prompt
    assert "port 8750" in prompt and "do not stop any process you did\n   not start" in prompt


def test_readme_agent_prompt_names_only_real_commands():
    from polybench.ai import cli
    prompt = _agent_section().split("```text")[1].split("```")[0]
    for c in re.findall(r"`(polybench[^`]*)`", prompt):
        parts = c.split()
        if len(parts) < 2:
            continue   # the bare word, as in "the `polybench` MCP tools"
        if parts[1:3] == ["ai", "setup"]:
            assert set(parts[3:]) <= {"--trial"}, c
        elif parts[1:3] == ["ai", "install-skills"]:
            assert set(parts[3:]) <= {"--project", "."}, c
        elif parts[1] == "example":
            assert parts[2] == "esp32-s3" and "--dest" in parts, c
        else:
            assert parts[1] in {"--help", "selftest", "--protocol"} or c.startswith("polybench-pcb"), c
    n = len([p for p in cli.SKILLS.iterdir() if (p / "SKILL.md").is_file()])
    assert f"copies {'five' if n == 5 else '???'} skill folders" in prompt, n


def test_readme_agent_prompt_uses_polybench_example_and_a_scratch_data_dir():
    prompt = _agent_section().split("```text")[1].split("```")[0]
    assert "clone of the repository" not in prompt
    run = next(c for c in re.findall(r"`(polybench[^`]*)`", prompt) if "--simulate" in c)
    assert "--data-dir" in run and "yd-esp32-s3.pcbv.yaml" in run
    assert "polybench example esp32-s3 --dest" in prompt
