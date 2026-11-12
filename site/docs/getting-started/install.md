# Install

Polybench is a Python package with a command-line entry point, `polybench`. It serves its user interface on your own machine; you use it in a normal browser.

## Requirements

| Item | Version or note |
|---|---|
| Python | 3.12 or newer |
| Operating system | Developed and tested on macOS. The core is plain Python; the camera scanner and some instrument checks use macOS facilities. |
| Browser | Any current Chromium, Safari or Firefox |
| Internet | Needed once, for the install. Not needed at the bench. |

Optional, depending on your bench:

| For | You need |
|---|---|
| Programming over SWD (`openocd.*`) | OpenOCD 0.12 or newer on the `PATH` (Homebrew: `open-ocd`) |
| CAN (`pcan.*`) | A PEAK PCAN-USB adapter and its driver (PCBUSB on macOS) |
| Keysight N6705B power analyzer (`n6705b.*`) | The separate `polystage-bench` instrument library <span class="badge planned">Planned</span>: not yet published, so there is no extra for it. The actions are registered and say so when it is missing |
| ECAD highlight | Autodesk Fusion with its MCP server enabled |
| Camera scanner, AOI | The `scanner` and `aoi` extras (OpenCV and friends) |

## Install the command

```bash
pipx install polybench-pcb-validation      # or: uv tool install polybench-pcb-validation
polybench --help
polybench doctor
```

Add optional features as extras: `pipx install "polybench-pcb-validation[scanner,aoi]"`. The extras are listed [below](#extras).

## Install from a checkout with the script

From the repository root:

```bash
scripts/install.sh
```

The script:

1. checks that Python is 3.12 or newer (set `PYTHON=/path/to/python3` to choose one);
2. creates a clean virtual environment in `.venv` (set `VENV=/some/path` to put it elsewhere);
3. installs Polybench with its core dependencies (FastAPI, Uvicorn, PyYAML, python-can, cantools) in editable mode;
4. ends with `polybench selftest`.

Add extras by name:

```bash
scripts/install.sh scanner aoi       # camera scanner and AOI in the Visual step
scripts/install.sh camera            # camera.snapshot action only
```

Expect the last line to read:

```text
selftest: 7/7 passed (HOST-ONLY: no hardware touched)
```

The self-test loads the demo protocol, writes and replays a journal, checks the limit classes, renders a report, lists the actions, runs an action in a child process and drives the simulated ECAD bridge. It touches no hardware.

## Install by hand

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[scanner,aoi]'
.venv/bin/polybench selftest
```

For development and the browser click-through tests add the `dev` extra: `.venv/bin/pip install -e '.[dev]'`.

## Extras

| Extra | Adds | Used by |
|---|---|---|
| `scanner` | OpenCV, zxing-cpp, openpyxl, Pillow, pillow-heif, AVFoundation bindings on macOS | the camera scanner in the [Assembly aid](../steps/assembly-aid.md) |
| `aoi` | OpenCV, Pillow, pillow-heif, NumPy | [AOI](../steps/visual-aoi.md) in the Visual step |
| `camera` | OpenCV | the `camera.snapshot` action |
| `dev` | pytest, Playwright, httpx | the test suite |

## Next

[Check the bench with doctor](doctor.md), then [run the demo](first-run.md).
