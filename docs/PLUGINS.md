# Polybench DV plug-ins

Status: version 0.1.0. Code: `src/polybench/plugins/`. Tests: `tests/plugins/` and `tests/test_doctor.py` (host only).

Install: `pipx install polybench-pcb-validation` or `pip install -e .` from a checkout. `python-can` and `cantools` are core dependencies; `opencv-python` is the `[camera]` extra (`camera.snapshot` only); `hidapi` is the `[dmm]` extra (`dmm.read` and the live multimeter card). The `n6705b.*` actions need the separate `polystage-bench` library, which is not yet published and has no extra; the actions are registered and say so when it is missing. System: `openocd` (Homebrew `open-ocd`), the PCBUSB driver for a PEAK PCAN adapter. Check everything read-only with `polybench doctor` (below).

## Conventions every action follows

- `ctx.propose(test_id, value, unit, evidence, raw)` is the only output that matters. **Which test receives which value is set in the action's `params`**, never hard-coded: `map: {key: test_id}` for fixed outputs, `propose: [{test, signal, stat}]` for the CAN actions. A key that is not mapped is simply not proposed. List the same test IDs in the action's `proposes:` so the core does not warn.
- No verdicts. Limits and pass/fail stay in the core.
- Each action owns its timeouts (no external kill). Cancel is honoured between steps; hardware is left safe on every exit path.
- `safe_state()` is called in a fresh child with no params. Actions that hold nothing say so; `n6705b.*` reconnects (to the instrument of the last run, or the simulator if the last run was simulated) and switches all outputs off with a readback.
- Evidence files go to `ctx.workdir`, named `<board>_<kind>_<timestamp>.<ext>`.
- Simulator: `ctx.simulate` true gives realistic values with no hardware and no network. Every `pcan.*` action decodes simulated frames through the same DBC / decode map as real ones; what a healthy board would send is described in a `sim:` param (see `pcan.capture`). The ECAD bridge `fusion` simulates when built with `simulate=True` or env `POLYBENCH_SIMULATE=1`; the core uses its own `SimulatedEcad` under `--simulate`.

## ECAD bridges (`polybench.ecad`)

| Name | Does | Readiness |
|---|---|---|
| `fusion` | `status()` names the open document, active editor and part count; `designators()` lists the parts of the active board or schematic (frames and supply symbols removed); `highlight([...])` sends one EAGLE `SHOW name ...` to the open Electronics document through Fusion's MCP server at `http://127.0.0.1:27182/mcp`; `clear()` returns Fusion to the Select tool. Designators not in the document are returned in `missing`; net names (VIN, 3V3) are passed through. Refuses names that are not plain EAGLE names. Never touches a command the operator opened (returns `busy`). Optional guard: `set_expected_document("DEMO-1")` or env `POLYBENCH_FUSION_DOC`. | HARDWARE-VALIDATED (15 Oct 2026, a 57-part board open in Fusion: status, designators read, `SHOW C1 C10` accepted, cleared). The highlight itself was not looked at by the agent. |
| `kicad` | KiCad 10 through its IPC API (kicad-python, optional extra `kicad`, local nng socket `/tmp/kicad/api.sock`). Step 1 (`status`, `documents`, `designators`, expected-document guard), plain highlight and, through the scanner client `polybench.kicad.client.KicadScannerClient`, the test highlight (parts, `REF.PIN` pads, nets as pads), the Assembly aid, board choice and the polarity card's placed footprints. Read-only: reads footprints, pads and nets, and sets or clears the PCB editor selection; nothing is edited, saved or moved. Choose it with `--ecad kicad` (or env `POLYBENCH_ECAD=kicad`): that selects the bridge and the scanner client together, and Fusion is never contacted. Details below. | HARDWARE-VALIDATED, 17 Oct 2026, KiCad 10.0.7, a test board (selection read back through the API after every call, files byte-identical). The selection, zoom and net highlight were looked at in screenshots by the implementer, not yet by the maintainer's eye; the busy, no-board and KiCad-quit states are UNVALIDATED live (they need an operator at KiCad). |
| `altium` | Interface only. `status()` says "not installed"; `highlight()` returns `ok: false`. | Stub |

Result of `highlight`: `{ok, highlighted, missing, message, command, document, editor}`.

### KiCad bridge (`kicad`)

- **Needs:** KiCad 10 running with the board open in the PCB editor, and the API server switched on (KiCad, Preferences, Plugins, enable the KiCad API server; key `api.enable_server` in `kicad_common.json`; restart KiCad). `pip install -e '.[kicad]'` (pins `kicad-python==0.8.0`). `polybench doctor` shows a "KiCad API" line that names whichever of these is missing. Polybench reads the settings file and never writes it.
- **One connection:** all KiCad I/O of the process goes through one connection on one worker thread, each call with a hard time budget (status 4 s, selection 6 s, reads 10 s, below the server's 5 / 8 / 12 s bounds) and an nng request timeout of 2000 ms. No socket file means no dial. Only a local `ipc://` socket is accepted. The server never waits on KiCad.
- **Names:** a name is matched exactly (case-insensitive) against KiCad's references, never guessed (`OSC0` is not `OSC`). A name that is not a reference but is a net selects all pads of the net. `REF.PIN` selects that pad. A pad of a selected footprint is left out of the request because KiCad drops it. An optional alias table `design.ecad_aliases: {OSC: OSC0}` in the protocol maps plan or BOM names to board names before matching; the result shows `OSC→OSC0`. Default: empty.
- **Excluded footprints:** `designators()` leaves out references starting with `#` and footprints marked exclude-from-BOM or not-in-schematic (env `POLYBENCH_KICAD_EXCLUDE`: comma list of `hash,bom,not_in_schematic`, or `none`). `status().excluded` lists them.
- **View options** (env `POLYBENCH_KICAD_ACTIONS`, default `zoom,highlight`; `schematic` is opt-in; `none` turns all off): after a selection the plug-in runs KiCad view actions through `RunAction`: `common.Control.zoomFitSelection` (`centerSelection` for one item) and `pcbnew.EditorControl.highlightNetSelection` when pads are selected (the net is highlighted in the PCB editor and cross-probed to the schematic), `clearHighlight` on clear, and with `schematic` also `pcbnew.InteractiveSelection.SelectOnSchematic`. Only these five names pass the guard, none edits anything, and a failed or busy action is reported in the result and never fails the selection. KiCad calls `RunAction` an unstable API: a KiCad update may rename an action; the result then says `RAS_INVALID`.
- **Schematic:** the API has no schematic selection in KiCad 10. KiCad's own cross-probing carries the PCB selection into the open schematic editor (settings `cross_probing.on_selection`, `center_on_items`, `zoom_to_fit` in `eeschema.json`, all on by default here).
- **Allow-list:** the only API commands sent are GetVersion, Ping, GetOpenDocuments, GetItems, GetItemsById, GetItemsByNet, GetNets, GetSelection, AddToSelection, RemoveFromSelection, ClearSelection and the five view actions. Everything else is refused before it is sent.
- **Simulator:** `--simulate --ecad kicad` runs the same code over an in-memory board and a fake of KiCad's API server; no socket is opened.

## `openocd.identify`

A debug probe and a SAM D/L/C21 target over SWD with OpenOCD. The probe (`atmel-ice` default, `cmsis-dap`, `stlink`, `jlink`) and the target are parameters: see [plugins/openocd.md](plugins/openocd.md) for presets, labels, wiring and failure messages. pyOCD is not used.

- **Default probe: Atmel-ICE only.** Both OpenOCD actions put `cmsis_dap_vid_pid 0x03eb 0x2141` between the interface and target scripts, so OpenOCD never opens any other USB device (on 15 Oct it touched the PCAN adapter's descriptor). `vid_pid: [vid, pid]` overrides it.
- Params: `probe_serial` (needed only if two probes), `speed_khz` (1000), `openocd` (path), `config` (path to one OpenOCD config file that replaces the default interface + target, for example the nRESET variant `atmelice_samc21_srst.cfg`, which carries its own vid/pid filter), `map` (below).
- Proposes (through `map`): `probe_serial`, `device_id` (DSU DID as `0xXXXXXXXX`), `flash_kb`, and from the NVM user row and serial number: `bootprot` (bits 2:0), `bodvdd_level` (bits 13:8), `bodvdd_enabled` (Y when bit 14 is clear), `nvm_user_row` (word 0 as hex), `uid` (the four serial words as 32 hex characters, word 0 first).
- Addresses (SAM C20/C21 DS60001479D): NVM user row at `0x00804000`, section 9.3 and Table 9-4 (BOOTPROT production value 7, BODVDD level production value 8, disable bit 14 = 0). Serial number, section 9.6: words at `0x0080A00C`, `0x0080A040`, `0x0080A044`, `0x0080A048`. Memory reads only (`mdw`), the core keeps running. The raw words are saved as `<board>_identify_<ts>.json` (evidence for each proposal). Anything not read is logged and not proposed, never guessed.
- Errors in plain words: no programmer found; programmer cannot be opened; target not responding (power or SWD wires: the Atmel-ICE gives no target voltage through OpenOCD, so the two cannot be told apart); device locked (security bit); two probes connected; OpenOCD not installed; OpenOCD timed out (15 s, then the process group is stopped).
- Simulator: probe `J41800012345`, DID `0x11001106`, 256 KiB, production NVM row (BOOTPROT 7, BODVDD level 8, enabled) and a fixed made-up serial.
- Readiness: HOST-ONLY with a fake OpenOCD. The `cmsis_dap_vid_pid` command was confirmed to exist in the installed OpenOCD 0.12.0. The DID register address is read at both `0x41002018` and `0x41002118` and the one that decodes as a Cortex-M0+ part is used; the NVM row and serial reads have never run against silicon.

## `openocd.flash`

- Params: `image` (ELF or HEX; `.bin` is refused because it has no load address), `probe_serial`, `speed_khz`, `timeout_s` (90), `config`, `vid_pid` (as above), `map: {flashed, image_sha256}`.
- Runs `program <image> verify reset`, requires `** Verified OK **`. Proposes `flashed = Y` and the full SHA-256 of the image only after a verified flash; a failed verify raises and proposes nothing.
- Simulator: hashes the real file, pretends to flash.
- Readiness: HOST-ONLY. Never flashed a board.

## `openocd.read_ram`

Reads a block of target RAM over SWD without halting or resetting, and proposes fields of it. A self-test image keeps its results in a RAM struct, so no CAN adapter is needed. Probe params as `openocd.identify`.

- Params: `address` (int or hex) or `map_file` + `symbol` (GNU ld map, resolved at run time), `length` (bytes, 8), `ready: {byte, not}` (poll until that byte differs from `not`), `wait_s` (20), `fields`.
- Field `{test, byte, bit}` proposes `1`/`0`; `{test, byte}` proposes the byte value.
- Numeric field `{test, byte, width, ...}`: `width` 1, 2 or 4 bytes; `signed` (false); `endian` `little` (default) or `big`; `scale` (1) and `offset` (0), value = raw * scale + offset, proposed as a decimal string; `decimals` (optional, fixed number of decimals, otherwise up to 12 significant digits); `invalid` (optional raw value meaning not measured: nothing is proposed and a warning is logged); `sim` (optional number in physical units, simulator only, default 0).
- A field outside the `length` read, or a width other than 1, 2, 4, is a parameter error before the probe is touched.
- Simulator: encodes each numeric field's `sim` value into the buffer and reads it back through the same path.
- Readiness: HOST-ONLY.

## `pcan.capture`

Python-can on PCAN (`can.Bus(interface="pcan", channel, bitrate)`, 200 ms settle).

- Params: `channel` (`PCAN_USBBUS1`), `bitrate` (1000000), `listen_only` (false; true refuses to transmit), `filters` (default: the IDs of the decoded signals), `duration_s` (5; 30 when `samples` is given; max 600), `samples`, `discard` (leading samples dropped), `send: [{id, data, extended, delay_ms}]` and `settle_ms` (frames sent before capture, to set a mode), signals from a DBC (`dbc` + `signals`, `Message.Signal` if ambiguous) or from `decode: [{name, id, extended, start, length, little_endian, signed, scale, offset, unit, float}]`, and `propose: [{test, signal, stat, unit, scale, digits}]` with `stat` one of `last mean std rms_noise pp min max rms n rate_hz` and the three families below.
- Saves every received frame, with the decoded columns, to `<board>_capture_<ts>.csv` (evidence of each proposal).
- Error when nothing arrives lists what to check (node power and image, bitrate, wiring, termination at the adapter end, IDs).
- **Simulator**: `sim: {rate_hz, rates, seed, values, drop, error_frames}`. `rate_hz` is per message and every message runs on its own schedule; `rates: {signal or 0xID: hz}` overrides one message (a signal name selects the message it is in). `values: {signal: number | {mean, sd} | {counter: modulus, start}}` in physical units; a counter signal increments with every frame of its message and wraps at the modulus. `drop: [n, ...]` removes the nth periodic frames (the counter still advances, so the receiver sees a gap). Virtual time: a 60 s capture takes milliseconds. A protocol uses `sim:` to describe a healthy board so a simulated batch gives plausible values; it is ignored on real runs.
- Readiness: HOST-ONLY. Bit extraction is checked against cantools for Intel and Motorola signals.

## `pcan.stats`

Same bus params as `pcan.capture`, for one signal.

- Params: `signal`, `full_scale` (full-scale **span** in the signal's unit, e.g. 2 x Vref / gain), `unit`, `display_scale` and `display_unit` (e.g. 1e6 and `uV`), `digits`, `window` (samples, for the windowed pk-pk), `counter: {signal, modulus}` (for sequence gaps), `map: {...}` from stat to test ID.
- Definitions (ADS datasheet convention): `std` = `rms_noise` = sample standard deviation; `pp` = max - min over the whole capture; `noise_free_bits` = log2(FS / pp); `effective_bits` = log2(FS / rms_noise); `rate_hz` = (n-1) / (t_last - t_first) from the receive timestamps. A signal that never changes proposes no bits and logs a warning.
- **Windowed pk-pk**: `pp_window` = mean of the pk-pk of consecutive, non-overlapping `window`-sample blocks (the typical figure a datasheet quotes, comparable with a 450-sample table value, where the whole-capture `pp` runs about 25 % larger); `pp_window_max` = the worst block; `noise_free_bits_window` = log2(FS / `pp_window`), needs `full_scale`. A trailing partial block is ignored; fewer than `window` samples logs a warning and proposes nothing.
- **Sequence gaps**: `seq_gaps` = lost frames of the `counter` signal, the sum over consecutive samples of `((delta mod modulus) - 1)` for non-zero deltas, so it is right across a wrap; `seq_repeats` = identical consecutive values (a duplicate, or exactly one full wrap lost). `pcan.capture` has the same stats per `propose` entry with a `modulus` key.
- **Error frames**: `error_frames` = error frames python-can received. The macOS PCBUSB library does not support error frames (python-can says so), so there the stat is not proposed and a warning says a 0 would prove nothing. It is available in the simulator (`sim.error_frames`) and on platforms that report them.
- Saves `<board>_stats_<ts>.csv` and `.json`.
- Readiness: HOST-ONLY.

## `pcan.query`

One request/reply, or several on one open adapter.

- Params: `request {id, data, extended}` (omit to just wait for a frame), `response {id, extended, prefix}` (required), `timeout_s` (2, max 30), `retries` (0), decode as above, `propose: [{test, signal, as: number|hex|yn, unit, digits}]`. `hex` proposes the raw value as `0x..`; `yn` proposes `Y` when the value is nonzero.
- **Several requests**: `queries: [{title, request, response, decode | dbc+signals, propose, timeout_s, retries, sim}]`. Channel, bitrate, timeout and retries at action level are the defaults. Every query is tried even if an earlier one got no reply; the proposals of those that answered are kept, then the action fails and names the queries without a reply. Each query decodes its reply with its own signal list (several queries can answer on the same ID).
- Error names the response ID and the attempts when no reply arrives.
- **Simulator**: the node answers every request with a frame that starts with the response `prefix` bytes and carries the proposed signals at their decoded bit positions, valued from `sim.values` (physical units, default 0), so the reply decodes through the same decode map as a real one. This replaced the all-zero reply that never matched a prefix and made six function actions fail under `--simulate`. `sim.reply` / `sim.replies` (`{request_id, request_data, id, data}`) still take priority; a query's `sim: {no_reply: true}` keeps the simulated node silent for it.
- Readiness: HOST-ONLY.

## `n6705b.identify`, `n6705b.measure`, `n6705b.first_power`, `n6705b.load_sweep`

Through the `polystage-bench` library, which owns the safety rules: limits come from a DUT profile TOML (`profile`: name in `~/.config/polystage-bench/profiles` or a path), never from the params. A param may only ask for a lower current limit. Outputs are switched off and read back on every exit path (done, error, cancel). One session per action. Instrument: `instrument` (default: the only one in `instruments.toml`), optional `host`, `serial`, `subnet`, `timeout_s` (5), `patience_s` (12).

| Action | Params | Proposes |
|---|---|---|
| `n6705b.identify` | `map: {idn, serial}` | the IDN text. Read only. |
| `n6705b.measure` | `channel` (1), `samples` (5), `current_unit` (`mA`), `output` (omit, `off` or `voltmeter`), `map: {voltage, current, power}` | V, I, P as the mean of the readings. Changes nothing; warns if the output is off. `output: voltmeter` puts the channel in voltmeter emulation (`EMUL VMET`, read back, output on, read, output off, read back) and reads the rail it is wired to; it sources nothing, proposes only `map.voltage`, needs `voltmeter = true` on that channel in the profile and a channel that is OFF. `output: off` reads a channel without enabling it, is refused if the channel is ON, and is refused as invalid when it reads below 0.05 V: an OFF N6781A reads 0 V on this frame (19 Oct, firmware D.02.13), so use `voltmeter`. |
| `n6705b.first_power` | `profile` (required on real runs), `channel`, `volts` (required), `ilim_a` (lower only), `ramp {start_v (1.0), step_v (0.25), dwell_s (0.2)}`, `settle_s` (1), `samples` (5), `leave` (`off` default or `on`), `current_unit`, `map: {voltage, current, power}` | V and I after settling. Current limit = profile limit; OVP = lower of the profile OVP and setpoint + 15 % (at least 0.3 V). Warns when the supply sits in current limit. |
| `n6705b.load_sweep` | `profile` (required on real runs), `source_channel` (1), `load_channel` (2), `volts` (required), `points_ma` (ascending, first 0, required), `dwell_s` (0.5), `samples` (5 per point), `settle_s` (1), `guard_v`, `stop_below_v` (0.5), `max_total_s` (60, never more), `ilim_a` (lower only), `ramp`, `current_unit`, `map` | `rail_v_noload`, `rail_v_fullload`, `rail_v_min`, `rail_v_max`, `regulation_mv` (no-load minus full-load rail), `input_ma_fullload`, `efficiency_pct_fullload`, and `points_table` (every point as a table). Every point is also written as CSV and JSON in the run folder (the proposals carry the CSV path as evidence). |

- `leave: on` lasts only until the frame watchdog (profile `watchdog_s`) expires: nobody keeps the session alive after the action, by design.
- Latched protection: before it energises, `first_power` and `load_sweep` read the frame's protection state and name any latch in plain words (OV, OC, PF, OT, INH, PROT, OSC). `first_power` clears a latch that is the output watchdog alone (only PROT, protection coupling off, every output off) and logs `the previous hold window expired (watchdog); cleared`; any other latch is refused with `clear it on the front panel after checking the board`. `load_sweep` never clears, it refuses. `measure` only warns. `first_power` with `leave: on` logs at the start that the outputs switch off by themselves at the profile's `watchdog_s` and that the frame shows a watchdog protection afterwards.
- Profile key (polystage-bench): `voltmeter = true` on a channel entry allows voltmeter emulation on that channel; `mode = "VMET"` makes the channel a voltmeter only (it can never be energised as a source or load).
- `load_sweep` sequence: every request is checked against the profile before anything is energised (source volts and current, load current against `max_sink_a`, `guard_v` against the load channel's voltage limit, output ranges per `output_range_v`; a plan whose estimated time, settle plus points times dwell and samples plus overhead, exceeds `max_total_s` is refused). The source comes up and settles; the rail is then read in **voltmeter mode** on the load channel (an OFF N6781A reads 0 V, so a plain read would always pass the guard); above `guard_v` the load is not enabled and everything is switched off. That reading is also the 0 mA point. Only then is the channel switched to CCL for the load points. The profile entry of the load channel must say `voltmeter = true`. The N6781A refuses a sink bound in CCL (`CURR:LIM:NEG`, error +326 on firmware D.02.13), so this rail check is what protects the load. The load's voltage limit is programmed to `guard_v`. Then the load steps through `points_ma`; each point averages `samples` readings of the rail (load channel) and of input V and I (source channel). It aborts, all outputs off and read back, on a rail below `stop_below_v` or above `guard_v`, a protection trip, the run exceeding `max_total_s`, a link failure or cancel. Efficiency is P_load / P_in with P_load = rail V times the measured load current; empty at 0 mA. A partial sweep still writes its CSV and proposes nothing.
- Simulator: the library's `SimN6705B`; `sim_current_ma` sets the simulated load (default 20 mA). `load_sweep` adds a sagging rail on top of it: `sim_rail_v` (3.3), `sim_rail_ohm` (0.05), `sim_eta` (0.9), `sim_quiescent_ma` (1), `sim_collapse_ma` (the rail falls to 0.4 V at and above this load). A missing profile file falls back to the safe default only in the simulator.
- Readiness: `identify` HARDWARE-VALIDATED (15 Oct). `measure`, `first_power` and `load_sweep`: HOST-ONLY (simulator, outputs never switched on in a real run).

## `camera.snapshot`

- Params: `index` (0), `width`, `height`, `warmup_frames` (5), `quality` (90), `map: {evidence}`. Saves a JPEG to the run folder and proposes the file name with the path as evidence.
- OpenCV is not installed on this Mac (`pip install opencv-python`); the action says so. Simulator writes a generated JPEG.
- Readiness: HOST-ONLY.

## `dmm.read`

One stable reading of the UNI-T UT161E over the CH9329 cable (extra `dmm`, `import hid`). Full description, facts and sources: [MULTIMETER.md](MULTIMETER.md).

- Params: `timeout_s` (10), `unit` (plan unit to propose in; same quantity as the reading, else the action fails and names both), `n` (5) and `pct` (0.2) for the stability rule, `map: {value: <test_id>}`, `sim: {scene, value, mode, range}` for the simulator.
- Waits for `n` consecutive readings within `pct` % of their mean (floor one count), same decode, per-model table and rule as the live card. Overload, HOLD, REL, MIN, MAX, P-MIN, P-MAX and AC readings never count; on timeout the action fails and says which of these it saw.
- One owner of the HID handle: the server pauses its live reader while the action runs and resumes it afterwards. Only Get Name and Get Measurement are sent.
- Simulator: `SimTransport` replays scripted frames through the same framing and decode path.
- Readiness: HOST-ONLY for the stable-reading path. HARDWARE-VALIDATED for the mechanism only: on 27 Oct, with the dial on Ohm and open leads, a run from the server paused the live reader, opened the real cable, read `OL.` for 4 s, failed with "no stable reading within 4 s: overload", closed the cable and resumed the reader.

## `polybench doctor`

Read-only preflight, run before a session: `polybench doctor --protocol <your protocol file>`. Lines are green (OK), amber (WARN: optional or manual-entry), red (FAIL: something the protocol needs) or INFO (nothing asked). Exit code 1 if anything is red. Nothing is written to a device, no CAN frame is sent, no output is touched, no process is signalled.

**The checks follow the protocol.** A programmer, CAN adapter, power analyzer or ECAD bridge is checked only if an action (`openocd.*`, `pcan.*`, `n6705b.*`) or a capability (`ecad`) of the loaded protocol references it. A capability declared `optional` turns every red line that depends on it into amber: its package, plug-in, OpenOCD, probe, PCAN adapter, firmware images, DBCs and profile files. An action's `image` param (any plug-in: `openocd.flash`, `esptool.flash`, ...) gets the image existence, `image_sha256`/`sha256` and `MANIFEST.txt` check. With no protocol the doctor lists what is installed and raises no red line for hardware. A plug-in without a host-side preflight gets an INFO line.

| Check | Runs when | Red when | Amber when |
|---|---|---|---|
| Python, venv | always | Python < 3.12 | not in a venv |
| Packages | always listed | a package the protocol's actions need is missing (python-can for `pcan.*`, cantools for DBCs, polystage-bench for `n6705b.*`) | |
| Action plug-ins | always | a plug-in the protocol names is not registered or does not load | |
| OpenOCD, probe | `openocd.*` actions; one line per probe preset used and `vid_pid` (an action's `vid_pid` is the id looked for) | not installed; probe not on USB | older than 0.12; two probes; a named `probe_serial` absent |
| PCAN adapter | `pcan.*` actions | not on USB, or a process has the PCBUSB driver loaded (named by pid, never touched), or opening failed | |
| N6705B | `n6705b.*` actions **and** `--n6705b-host IP` or `--check-n6705b` (otherwise one INFO line, nothing is sent) | | no answer: one `*IDN?` on port 5025, 3 s, outputs untouched; no built-in host |
| ECAD bridge | the protocol declares an `ecad` capability, or `--ecad` is given (`fusion` or `kicad`, default `POLYBENCH_ECAD` else `fusion`) | | not reachable, no document, or a document without the `--expect-doc` text (no default) |
| Protocol, firmware images, DBCs | with `--protocol` | lint errors; an image missing; a hash differs from the protocol or `MANIFEST.txt`; a DBC unreadable or a signal missing | lint warnings; an image not in a manifest |
| Multimeter | always | `dmm.*` is used and hidapi or the CH9329 cable is missing | |
| Data folder, port | always | folder exists and is not writable (tested with `os.access`; nothing is created or written; a missing folder is INFO; `--data-dir DIR` or `POLYBENCH_DATA` picks it) | port in use |

With a free adapter the PCAN check opens it in **listen-only** mode (no ACK, nothing transmitted) and releases it at once; `--no-open` skips that.

## Readiness of the instrument plug-ins

Labels: HARDWARE-VALIDATED means the plug-in did what it should against the real instrument; HOST-ONLY means it passed on a computer with a simulator; UNVALIDATED means a real run is still to do.

| Plug-in | State |
|---|---|
| `fusion` | HARDWARE-VALIDATED: status, designators and highlight accepted, then cleared; the document was not marked modified. The highlight was not inspected by eye. |
| `kicad` | HARDWARE-VALIDATED on KiCad 10.0.7, read-only (see the table above). |
| `openocd.identify` | HARDWARE-VALIDATED for the no-probe path only: with no probe attached, real OpenOCD 0.12 ran and the action reported "No programmer found". The target-found and target-not-responding paths are UNVALIDATED. |
| `openocd.flash` | HOST-ONLY. It has never flashed a board. |
| `pcan.*` | UNVALIDATED against a node. The adapter enumerates on USB; the first open was blocked because another program held the channel, and the error text now names the holder. |
| `n6705b.identify` | HARDWARE-VALIDATED: one `*IDN?` over LAN returned the instrument identity and its modules; no output command was sent. |
| `n6705b.measure`, `n6705b.first_power` | HOST-ONLY. |
| `camera.snapshot` | HOST-ONLY. |
| `dmm.read` | HOST-ONLY (simulated meter). Device mechanism and `OL.` handling HARDWARE-VALIDATED on a UT161E, Ohm dial, open leads; numeric readings and all other modes UNVALIDATED. |

## Notes

1. Pass `proposes:` lists and the `map` / `propose` params together in each protocol action; the plug-ins read the mapping from the params.
2. `safe_state()` has no params in the contract; `n6705b.*` remembers the last connection in `~/.cache/polybench/n6705b_last.json`.
3. The CAN actions need the adapter free: stop any other program that holds the channel first.

## Revision

| Rev | Date | Change |
|---|---|---|
| 1 | 2026-10-27 | Added `dmm.read`, the `dmm` extra and the doctor multimeter line (T6.8). Earlier revisions were not tracked here. |
