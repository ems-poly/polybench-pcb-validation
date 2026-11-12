"""Lane R (C14, C15): the ESP32-S3 example ships in the wheel and `polybench example` copies it. HOST-ONLY, silent."""
from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from polybench import example_cmd
from polybench.cli import main as cli_main

ROOT = Path(__file__).resolve().parents[1]
PKG_EX = "polybench/examples/esp32-s3/"


def test_is_shipped_rule():
    ok = ["yd-esp32-s3.pcbv.yaml", "YD_ESP32_S3_TEST_PLAN.md", "assembly.yaml", "kicad/yd-esp32-s3/yd-esp32-s3.kicad_sch",
          "kicad/yd-esp32-s3/yd-esp32-s3.kicad_pcb", "aoi/layout/layout.json"]
    no = ["screenshots/a.png", "firmware/prebuilt/x.factory.bin", "kicad/yd-esp32-s3/evidence/board_render.png",
          "kicad/yd-esp32-s3/evidence/board_kicad-cli.svg", "kicad/yd-esp32-s3/tools/build_pcb.py", "firmware/.gitignore", "x/y.bin"]
    assert all(example_cmd.is_shipped(r) for r in ok)
    assert not any(example_cmd.is_shipped(r) for r in no)


def test_shipped_set_has_yaml_plan_bom_kicad_and_no_png_or_bin():
    src = ROOT / "examples" / "esp32-s3-board"
    files = example_cmd.shipped_files(src)
    assert "yd-esp32-s3.pcbv.yaml" in files and "YD_ESP32_S3_TEST_PLAN.md" in files
    assert "assembly.yaml" in files                      # the BOM is its `bom:` block
    assert any(f.endswith(".kicad_sch") for f in files) and any(f.endswith(".kicad_pcb") for f in files)
    assert not [f for f in files if f.lower().endswith((".png", ".bin", ".hex", ".elf")) or f.startswith("screenshots/")]


def test_cli_example_copies_and_prints_run_command(tmp_path, capsys):
    dest = tmp_path / "ex"
    assert cli_main(["example", "esp32-s3", "--dest", str(dest)]) == 0
    out = capsys.readouterr().out
    assert (dest / "yd-esp32-s3.pcbv.yaml").is_file() and (dest / "kicad/yd-esp32-s3/yd-esp32-s3.kicad_sch").is_file()
    assert not (dest / "screenshots").exists() and not (dest / "firmware/prebuilt").exists()
    assert f"polybench --protocol {dest.resolve() / 'yd-esp32-s3.pcbv.yaml'} --simulate" in out
    assert "--data-dir" in out
    from polybench.protocol import load
    assert load(dest / "yd-esp32-s3.pcbv.yaml").data["design"]["name"] == "YD-ESP32-S3"   # the copy loads on its own


def test_cli_example_refuses_non_empty_dest_and_unknown_name(tmp_path, capsys):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "keep").write_text("1")
    assert cli_main(["example", "esp32-s3", "--dest", str(tmp_path / "x")]) == 2
    assert (tmp_path / "x" / "keep").read_text() == "1"
    assert cli_main(["example", "nope"]) == 2
    assert cli_main(["example"]) == 2


def test_demo_automated_is_not_in_the_package():
    assert not (ROOT / "src/polybench/examples/demo_automated").exists()


def _build_wheel(tmp_path):
    out = tmp_path / "whl"
    r = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-q", "-w", str(out), str(ROOT)],
                       capture_output=True, text=True, timeout=120)
    if r.returncode:
        pytest.skip("cannot build a wheel here (needs setuptools>=77 in this environment): " + r.stderr[-300:])
    return next(out.glob("*.whl"))


def test_wheel_contains_the_example_and_nothing_heavy(tmp_path):
    names = zipfile.ZipFile(_build_wheel(tmp_path)).namelist()
    ex = [n for n in names if n.startswith(PKG_EX)]
    assert PKG_EX + "yd-esp32-s3.pcbv.yaml" in ex and PKG_EX + "YD_ESP32_S3_TEST_PLAN.md" in ex and PKG_EX + "assembly.yaml" in ex
    assert any(n.endswith(".kicad_sch") for n in ex) and any(n.endswith(".kicad_pcb") for n in ex)
    assert not [n for n in ex if "/screenshots/" in n or n.lower().endswith((".png", ".bin", ".hex", ".elf"))]
    assert not [n for n in names if "demo_automated" in n]
    assert any(n.startswith("polybench/examples/demo/") for n in names)
