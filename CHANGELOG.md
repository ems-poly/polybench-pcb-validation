# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/). Before 1.0, minor versions may change the protocol, plan and plug-in formats; such changes are listed under **Changed**.

## [0.1.0] - unreleased

First public release. Alpha.

### Added

- Windows core (UIR-56 W1, work in progress, HOST-ONLY): `polybench/platform.py` holds every operating-system difference (folders, process groups and process-tree stop, process lookup, file manager, serial port names). Cancelling an OpenOCD or esptool action, or the app going away, stops the programmer and everything it started on macOS, Linux and Windows (tested on all three in CI). Every text file is read and written as UTF-8 (a reading such as "10 kΩ" could crash the action log on Windows). Optional extra `process` installs `psutil`; without it Polybench falls back to `ps`, `taskkill` and PowerShell. Windows paths such as `C:\fw\a.hex` are accepted for the OpenOCD image and config arguments.
- `polybench example esp32-s3 [--dest DIR]` copies the ESP32-S3 example (protocol, test plan, BOM, KiCad files, AOI layout and photos, firmware source; no screenshots, no firmware binary) out of the wheel and prints the run command. The README try-it section shows the package and clone paths. The unused `demo_automated` protocol moved from the package to `tests/data/` (a test fixture only).
- Design step 4: a **Copy agent prompt** button builds a prompt from the project (name, ECAD document, BOM, plan and draft paths, plan format pointers, the MCP tools to use) for the operator's own agent. It never includes notes or journal text. If no ECAD is linked the prompt says so and tells the agent to ask. HOST-ONLY. Phase 2 (running an agent in the background) is a design note only: `docs/AGENT_PROMPT_PHASE2.md`.
- Table results: a test of `kind: table` with columns, per-column limits, a verdict per row and a plot; `ctx.propose_table(...)` for plug-ins; the report prints each table. `demo.sweep` shows it in the demo project.
- Twelve-step browser workspace: Project, Batch setup, Assembly aid, Visual (AOI review), Unpowered and Powered testing, Program and function, Characterisation, Report. Steps 2 (Schematic analysis) and 3 (BOM and manufacturing) are planned.
- Protocol file (`pcbv.protocol/2`) and markdown test plan as master (`plan_format: 1`) with tiers, a decision gate and a power interlock. Limits carry a class (`hard`, `guess`, `tbr`, `record`) and a source; only a hard limit can fail a board.
- Append-only journal, flushed to disk before a value shows as saved; restart resumes the batch. Operator notes and errata candidates. Report as Markdown and HTML with a readiness label per step.
- Plug-in contract (`plugin_api.py`) and plug-ins: Fusion and KiCad ECAD bridges (read-only highlight), `openocd.identify` and `openocd.flash` (probe and target presets), `pcan.capture`, `pcan.stats`, `pcan.query`, `n6705b.*` (needs the separate `polystage-bench` library), `camera.snapshot`, demo actions.
- Camera Assembly scanner and AOI (optional extras `scanner`, `aoi`); hands-free voice (extra `voice`, Preview).
- `polybench` commands: serve (default), `lint`, `plan`, `selftest`, `doctor` (read-only, `--json`), `aoi-derive`.
- Everything local: the server listens on 127.0.0.1, no telemetry, no background network traffic.
- User manual in `site/` (MkDocs) built with `--strict`.
- Live multimeter (T6.8): the UNI-T UT161E over the CH9329 HID cable (extra `dmm`). A meter card on the test page shows the live reading, proposes a stable reading into the test in focus (source `meter`), and optionally auto-accepts it with a spoken read-back and Undo (new journal event `undo`). `dmm.read` action, `polybench doctor` line, `--simulate` meter. See `docs/MULTIMETER.md`. Only a few modes are hardware-validated.

- First-run welcome page: `polybench` with no `--protocol` and no recent project opens on the Project page in a no-project state (New project, Open project, recent projects, **Try the demo project**) and a short list of the twelve steps. The same list stays on the Project page under **How Polybench works**. Every other endpoint answers `409 no project is open` until a project is open.
- Demo project: opens the packaged demo, always simulated, in its own folder under the user-data folder (`POLYBENCH_DEMO_DIR`), with SIMULATED and DEMO PROJECT in the app bar and a one-click way back. It is not added to the recent list and refuses a folder inside the data folder.
- Plugins tab in the bottom panel and `GET /api/plugins`: status (loaded, missing extra, missing library, import error), source, use by the protocol, read-only settings with secrets hidden, last result from the journal. No hardware is contacted.
- Hands-free bench commands (voice, Preview): "board S1" selects a board; "start test" reads the run back ("Run First power on S1, 12 volts. Say confirm.") and starts it on "confirm" within 10 s; "stop test" aborts the running action at once with the Stop button's safe state, never asks to confirm and is matched locally before the language model; "next test" and "previous test" move between rows. Every gate stays (step gate, power interlock, stops, one action at a time); every command, read-back, confirmation, refusal and timeout is journalled. Voice settings: `confirm_simulated`, `confirm_timeout_s`. Instrument readings are results at once, so "next step" accepts nothing for them, table results are read as rows and verdict, and "redo" is the per-test Redo path (value replaced, history kept). Simulator only until a real-hardware run.
- Guided section by voice (Preview): "guide me" (or a section's Guide button) walks the section step by step for the current board: "Step 1 of 3: Turn on 12 V. This turns on 12 volts, 100 milliamp limit. Say ready." "Ready" runs the step and counts as the spoken confirmation; the readings are read back ("5V rail: 5.02 volts, pass"), then "next step", "repeat", "redo", "skip step", "stop" (pauses the walk), "end guide". The walk is derived from the protocol (actions in order, then manual tests; optional `guide:` section key). A hold window after an action that leaves an output on is announced, warned at 15 seconds and offered for redo when it lapses. A guide panel on the page mirrors the voice. Every gate stays. Simulator only until a real-hardware run.

- `n6705b.load_sweep`: powers a DUT from one channel and steps a constant-current load on its rail from the profile's limits, with a rail guard read in voltmeter mode before the load is enabled, a hard 60 s plan limit, abort on rail collapse, protection trip, link loss or cancel, and every point as rail voltage, input V and I and efficiency in a CSV and JSON.
- `n6705b.measure` takes `output: voltmeter` (read the rail a channel is wired to in voltmeter emulation) and `output: off` (a channel that is off; refused if it is on, and refused as invalid when it reads 0 V, since an OFF N6781A reads 0 V on the frame).
- Latched protection is named in plain words before an action energises; `first_power` clears a watchdog-only latch, anything else is refused. `leave: on` logs when the outputs will switch off by themselves.

### Fixed

- `n6705b.load_sweep`: the rail guard and the 0 mA point read 0 V because an OFF N6781A does not read its terminals (found at the bench, 19 Oct); both now use voltmeter mode.

### Changed

- **Instrument readings are results** (19 Oct). A reading from an action plug-in is recorded in the cell as soon as it arrives, whatever its verdict, with its provenance (journal `accept` event with `auto: true`, input method `instrument`); nothing waits for Accept. Voice, assistant and LLM values stay proposals. An action can opt out with `record: propose`. A later reading of a recorded test (an action run again, or Redo) replaces that cell in the same attempt, the earlier value kept in its history; the other tests of the step are untouched. Only the explicit step retest opens a new attempt, and the power interlock needs it. Stop rules, power interlocks and gates act on recorded instrument values as on typed ones. Journals written earlier keep their pending proposals, acceptable by hand or with **Record N instrument readings**.
- Results and anomalies in the UI: verdict chips (PASS, RECORD, REVIEW, FAIL, STOP), source and time on every result, a per-board Results table, an Anomalies strip with reasons and **Redo** / **Add note**, rows that flash when an action records, "Proposed" cards with Accept (Enter) / Reject, and a layout that fits 390 px and dark mode.
- `polybench` with no recent project no longer exits with "no recent project to reopen".
- A project opened from the Project page takes the simulate mode of the process (`--simulate`), not that of the project that was open before.
- A bare `pip install` starts the app and walks every step. What needs an optional extra says `install polybench-pcb-validation[extra] to use this` (HTTP 424 from the server) instead of failing: OpenCV, NumPy, Pillow, zxing-cpp and openpyxl load on first use (`polybench/extras.py`).
- `polybench selftest` reads the demo protocol from package data (`polybench/examples/demo`), so it runs from an installed wheel.
- An action that is waiting on an operator question can be aborted from the question dialog (`Abort and go to safe state`); a plug-in that raises `polybench.plugin_api.Cancelled` ends as cancelled, not as an error, also under `python -m polybench.run_action`.
- The REVIEW message names the button that exists (`Accept for review`), and a toast moves off the controls instead of covering them.
- Protocols may carry `design.variant` (for example `N16R8`), shown after the design name.
- The simulators use neutral DEMO names. The esptool presence check no longer imports esptool (GPL-2.0-or-later); it is only run as a child process.
- The distribution is named `polybench-pcb-validation`; the command is `polybench`.
- There is no `bench` extra. It pointed at a local path that a public install cannot use. The `n6705b.*` actions stay registered and report that `polystage-bench` is not installed; see `docs/PACKAGING.md`.

### Known limitations

- Developed and tested on macOS. Linux runs the core and simulator and is not yet verified. Windows is not supported.
- Several plug-ins are HOST-ONLY (tested against simulators): see `docs/PLUGINS.md` for the label of each.
