# Packaging

Distribution name: `polybench-pcb-validation`. Command: `polybench`. Python 3.12 or newer. One package today; plug-ins live inside it and register through entry points (`polybench.actions`, `polybench.ecad`).

## Install

```bash
pipx install polybench-pcb-validation                 # or: uv tool install polybench-pcb-validation
pipx install "polybench-pcb-validation[scanner,aoi]"  # with extras
pipx inject polybench-pcb-validation kicad-python     # add a dependency later
```

From a source checkout: `scripts/install.sh [extra ...]`, or `python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"`.

## Extras

| Extra | Adds | For |
|---|---|---|
| (none) | fastapi, uvicorn, PyYAML, python-can, cantools | server, simulator, `pcan.*`, `openocd.*` |
| `camera` | opencv-python | `camera.snapshot` |
| `scanner` | opencv-python, zxing-cpp, openpyxl, pillow, pillow-heif, AVFoundation bindings (macOS) | camera Assembly scanner |
| `aoi` | opencv-python, pillow, pillow-heif, numpy | AOI in the Visual step |
| `voice` | sounddevice, numpy, mlx-whisper (macOS) | hands-free voice |
| `kicad` | kicad-python (pinned to the version verified against KiCad 10.0.7) | KiCad ECAD bridge |
| `dev` | pytest, playwright, httpx, kicad-python | tests |

## The `bench` extra is dropped (0.1.0)

Before 0.1.0 the `bench` extra was `polystage-bench @ file:///<a path on the maintainer's machine>`. A public install cannot resolve a local path, and PyPI rejects direct references in a package's metadata, so the extra could not be published.

Options considered:

| Option | Effect | Verdict |
|---|---|---|
| A. Drop the extra; keep the `n6705b.*` actions registered; the action reports "polystage-bench is not installed" and `polybench doctor` shows an amber line | Public install works; nobody gets a broken extra; the plug-in is visible and documented as needing a separate library | **Chosen for 0.1.0** |
| B. Publish `polystage-bench` to PyPI and restore `bench = ["polystage-bench>=X"]` | Best for users with the instrument; needs its own release, name check and licence review | Do after launch if the library is wanted publicly |
| C. Replace it with a first-party generic SCPI plug-in (the support proposal's `polybench-scpi`) | No private dependency; more work | The longer-term answer |
| D. Remove the `n6705b.*` plug-in from the public tree | Hides a hardware-validated plug-in | Not chosen |

Consequences of A: the `n6705b` entry points stay; the error message and docs say the library is separate and unpublished; the user manual's plug-in page already marks it Planned. Install hint in the action's error text should stop pointing at a local path (a core item, see the release checklist).

## Known packaging gaps (need product code, so not fixed in the release-prep branch)

- `polybench selftest` reads the demo protocol from package data (`polybench/examples/demo`, through `importlib.resources`), so it runs from a wheel as well as from a checkout (C11, fixed).
- `server.py` imports `polybench.aoi` (OpenCV, NumPy) unconditionally, so a base install may not start the UI; the extras are needed until that import is lazy (core item C1). The release install proof records what happens.
- The simulated Fusion recording in `assembly_scanner/` was missing from the wheel (fixed here with a package-data glob, `assembly_scanner/*.json`). It must be a synthetic DEMO-1 recording (`sim_demo_reads.json`) before release (core item C3).

## Later: a monorepo with plug-in packages

The support proposal (D4) describes one repository holding a core package, first-party plug-in packages (`polybench-kicad`, `polybench-openocd`, ...), a plug-in test kit and a plug-in index, with `PLUGIN_API_VERSION` checked by the host. That is a later step: it needs the plug-in API versioned first, and one maintainer gains little from it before there are outside plug-ins. Today's single package keeps the entry-point names, so plug-ins can move out without changing the protocol files. When it happens, `release.yml` changes from one tag (`vX.Y.Z`) to one tag per package (`<package>-vX.Y.Z`) and each package needs its own PyPI trusted publisher.

## Release mechanics

Version lives in `pyproject.toml` and `CHANGELOG.md` (the release workflow checks that they match the tag). Tags `vX.Y.Z` trigger `.github/workflows/release.yml`: build, then a manual approval in the `pypi` environment, then publish with trusted publishing. No PyPI token exists anywhere.
