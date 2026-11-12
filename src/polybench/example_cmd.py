"""`polybench example <name> [--dest DIR]`: copy a shipped example to a folder and print the run command.

The ESP32-S3 example lives in the repository at examples/esp32-s3-board/. Wheels carry a subset of it under
polybench/examples/esp32-s3/ (setup.py copies it at build time with `is_shipped`). From a source checkout (an
editable install, where the package folder has no copy) the same subset is taken straight from examples/.
Not shipped: screenshots, the prebuilt firmware binary, KiCad evidence renders and the KiCad build scripts.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

EXAMPLES = {"esp32-s3": {"repo_dir": "esp32-s3-board", "protocol": "yd-esp32-s3.pcbv.yaml"}}

_SKIP_DIRS = {"screenshots", "prebuilt", "evidence", "tools", "__pycache__"}
_SKIP_SUFFIX = {".png", ".bin", ".hex", ".elf", ".pyc"}


def is_shipped(rel: str) -> bool:
    """True when the example file `rel` (posix path inside the example) goes into the wheel and into a copy."""
    parts = rel.split("/")
    name = parts[-1]
    if name.startswith(".") or name == ".gitignore":
        return False
    if any(p in _SKIP_DIRS for p in parts[:-1]):
        return False
    return Path(name).suffix.lower() not in _SKIP_SUFFIX


def source_dir(name: str) -> Path | None:
    """Folder that holds the example: the installed package data, else the repository checkout."""
    meta = EXAMPLES.get(name)
    if meta is None:
        return None
    pkg = Path(__file__).resolve().parent / "examples" / name
    if (pkg / meta["protocol"]).is_file():
        return pkg
    repo = Path(__file__).resolve().parents[2] / "examples" / meta["repo_dir"]
    return repo if (repo / meta["protocol"]).is_file() else None


def shipped_files(src: Path) -> list[str]:
    return sorted(p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file() and is_shipped(p.relative_to(src).as_posix()))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="polybench example", description="copy a shipped example to a folder")
    ap.add_argument("name", nargs="?", help="example name: " + ", ".join(EXAMPLES))
    ap.add_argument("--dest", help="target folder (default: ./polybench-<name>-example); must not exist or must be empty")
    a = ap.parse_args(argv)
    if not a.name or a.name not in EXAMPLES:
        print(f"usage: polybench example <name> [--dest DIR]\nexamples: {', '.join(EXAMPLES)}", file=sys.stderr)
        return 2
    src = source_dir(a.name)
    if src is None:
        print(f"the {a.name} example is not in this install; use a source checkout (examples/{EXAMPLES[a.name]['repo_dir']})", file=sys.stderr)
        return 1
    dest = Path(a.dest or f"polybench-{a.name}-example").expanduser().resolve()
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        print(f"refusing: {dest} exists and is not an empty folder", file=sys.stderr)
        return 2
    files = shipped_files(src)
    for rel in files:
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, dest / rel)
    proto = dest / EXAMPLES[a.name]["protocol"]
    print(f"copied {len(files)} files to {dest}")
    print("run it with no hardware (use a scratch --data-dir so nothing lands in your own batches):")
    print(f"  polybench --protocol {proto} --simulate --data-dir {dest / 'data'}")
    print("not copied: screenshots and the prebuilt firmware binary (the flash step needs the clone or a PlatformIO build; --simulate does not).")
    return 0
