# Polybench: bench quickstart

One page for a bench batch. Everything you type is written to disk before it shows as saved. Your protocol file is the `.pcbv.yaml` of the project; below it is called `$PROTO`.

## Install (once, needs internet)

```
pipx install polybench-pcb-validation        # installs the `polybench` command
polybench doctor                             # read-only check of this machine
```

From a source checkout instead: `scripts/install.sh` (clean `.venv`, ends with `polybench selftest`, expect `selftest: 7/7 passed`). For the browser click-through tests as well: `.venv/bin/pip install -e '.[dev]'`.

In the commands below, `polybench` is the installed command; with a checkout use `.venv/bin/polybench`.

## Preflight (5 minutes)

1. **Fusion** is open on the board document of this project (the schematic or board editor, not another design). **KiCad instead of Fusion:** KiCad 10 is open on the project with the board in the PCB editor, the API server is on (KiCad, Preferences, Plugins, enable the KiCad API server, `api.enable_server` in `kicad_common.json`, then restart KiCad), the extra is installed (`.venv/bin/pip install -e '.[kicad]'`), and Polybench is started with `--ecad kicad` (or `POLYBENCH_ECAD=kicad`). `polybench doctor` has a "KiCad API" line. Nothing is edited or saved in KiCad; the selection, a zoom and a net highlight are what you see.
2. **Atmel-ICE** is on USB and the 10-pin cable is in its **SAM** port. Leave it off the board until the function step.
3. **PCAN-USB** is plugged in and **no other program holds it** (a soak or capture script, another CAN tool). Fit the **120 ohm termination at the PCAN end**: the node has none.
4. **Power:** the N6705B or a bench supply plus a meter.
5. Run the doctor with your protocol:

```
export PROTO=/path/to/your/protocol.pcbv.yaml
polybench doctor --protocol $PROTO
```

Green is good, amber is optional or manual entry (Fusion on another document, N6705B not answering), **red must be fixed**: it says what and how. A PCAN adapter that is held is named by program and pid; Polybench never stops it, close it yourself and run the doctor again. The doctor is read-only: nothing is written, transmitted or switched.

## Start

```
polybench lint $PROTO                                    # 0 errors expected
polybench --protocol $PROTO --batch <BATCH-NAME>         # real boards, e.g. DEMO-1-B1
polybench --protocol $PROTO --simulate --batch rehearsal # rehearsal, data labelled SIMULATED
```

The browser opens on http://127.0.0.1:8750/ (`--port N` to change). Real and simulated data never share a batch: use a different `--batch` name. Everything is in `~/polybench-data/<BATCH-NAME>/`: `journal.jsonl` (every accepted value, proposal, skip, retest, override, action event), `runs/` (evidence files, CSV of every capture), `reports/`.

## The flow

0. **Project (1).** The app opens here until the batch has a project. It shows whether Fusion is reachable (guided setup and **Check again** if not), the active document with its element count and part-list fingerprint, and the BOM loaded (file, line count). Make the right board the active tab in Fusion (Polybench never opens or switches a document), press **Use this document**, read the BOM cross-check (matched, missing in the design, missing in the BOM, DNF) and **Accept** it; a mismatch needs a note. No Fusion today: **Continue without ECAD** with a reason. The embedded Assembly aid reads the result from `GET /api/project`.
1. **Batch setup (5).** Type your name, check the board count, scan or type the serials (Enter moves to the next).
2. **Assembly aid (6).** Scan a reel barcode or type an MPN or designator in the big box: the BOM line, designators, polarity and DNF flags appear and the designators are highlighted in Fusion. Click designators as you place them, or press Enter for "all placed" and the next line. Ctrl+K jumps to the scan box.
3. **Visual (7)** is the AOI review only: drop the board photos on the board tiles (AOI photos), press **Run AOI on all photos**, then decide every issue (Y confirmed, N not an issue, U unsure) in Issue review. A board is done when every item is decided. A confirmed issue blocks Unpowered for that board until you press **Reworked** on it; Unsure only warns. There is no Yes/No checklist in this step. (**Live capture** is shown greyed out, Coming V2.)
4. **Unpowered (8), Powered (9).** Left panel lists the tests; board chips across the top; one test in focus. A test with pads or nets shows a **Probe card** with **Highlight in ECAD** (it also highlights when the test comes into focus). Type the reading and press Enter. A FAIL is never recorded by Enter: use the red **Record FAIL** button. **Shift+Enter** (or **Apply to all empty boards**) applies the typed value to every applicable board still empty for that test; a FAIL or stop-rule value shows a confirm panel first and records nothing until you accept it. A stop rule blocks the board until you acknowledge it with a reason. A step is gated until the previous one is complete without a FAIL (override needs a reason). Wrong board state? **Send S03 back for retest** records cause and fix and opens attempt 2; the first result stays in the report.
4. **Program and function (10).** Section buttons run an action on the current board (identify, flash, read over CAN). The bottom panel opens by itself and streams the output. Readings from an instrument action are **results**: they are recorded on the tests as the action delivers them, with the verdict chip and the source, and the rows flash. An out-of-band reading is listed in the **Anomalies** strip at the top; it does not block you. Move on to the next board, or press **Redo** to measure that test again (it replaces the value in the same attempt; the earlier value stays in its history). Values from voice or the AI assistant arrive as **Proposed** with Accept (Enter) and Reject. Abort stops the action and puts the instruments in the safe state. Some actions first ask you to confirm a set-up step (Continue / Cancel).
5. **Characterisation (11).** Same screen; each capture asks you to confirm the connection first, then reduces the capture to statistics and proposes them.
6. **Report (12).** Preview at any time. **Generate report files** writes `report.md` and `report.html` under `reports/`. Untested items are omitted; retests, overrides, TBR limits and open items are listed; the readiness label says HOST-ONLY, HARDWARE-VALIDATED or UNVALIDATED per step.

## Voice (hands-free)

Once: `.venv/bin/pip install -e '.[voice]'`, Ollama running (default model `ollama pull qwen3:8b`; until then the best installed model runs), and microphone permission for the terminal. Everything stays on this Mac.
Bottom panel **Voice** tab: **Voice on**, then hold the **`** key (or **Hold to talk**) and speak, or switch **Hands-free** on.
Say "S1 0.2 volts", "all boards 2 ohms", "accept", "accept fail" (a FAIL is never accepted by plain "accept"), "reject", "next test",
"board S4", "note R3 is 0603", "which test", "read out the test method", "show in Fusion", "progress", "repeat". Values arrive as
proposals; the transcript, the parser (grammar or LLM with its model) and the reply are in the Voice tab and the journal. Esc or
"cancel" stops a grace auto-accept. Settings: model, reply voice, grace, push-to-talk key. The mic is off when the page is hidden.
Details: `docs/VOICE.md`.

## Keys

Enter accept and advance (empty box: accept the proposal) | Shift+Enter apply to all empty boards | Esc clear | Up/Down test | Alt+Left/Right board | y n 1 2 3 quick answers | Ctrl+K scan box | Ctrl+B / Ctrl+J collapse left / bottom panel | ? help. The Advanced switch adds the full grid and journal hashes.

## If something fails

- **Nothing you accepted is ever lost.** Ctrl+C or a crash stops the server only; run the same start command again (same `--batch`) and every accepted value is back. The journal is `~/polybench-data/<BATCH-NAME>/journal.jsonl`; evidence files are beside it in `runs/`. "Server not reachable" in the top bar means the server stopped: restart it.
- **Port 8750 busy:** the tool says so and never kills anything. Close the other copy, or `--port 8751`.
- **PCAN "not free" or "channel not initialised":** another program has the adapter; the message names it. Close it yourself. Then `polybench doctor` again.
- **Atmel-ICE "No programmer found":** USB cable, SAM port, nothing else (Microchip Studio, another OpenOCD) holding it. **"Target not responding":** target power and the SWCLK, SWDIO, RST and GND leads.
- **No CAN frames:** node power and image, 120 ohm at the PCAN end, CAN_H/CAN_L not swapped, bitrate.
- **KiCad does not highlight:** the doctor's "KiCad API" line says why (extra not installed, API server off, KiCad not running, no board open, a dialog open in the PCB editor). The schematic follows the PCB selection through KiCad's own cross-probing.
- **Fusion does not highlight:** the Probe card says why; the app keeps working and manual entry always works. Check the right document is open.
- **N6705B does not answer:** the network connection; wait a minute and press the action again, or use the bench supply and type the readings. Outputs are switched off on every exit path and Abort.
- **An action hangs or looks wrong:** press **Abort** in the bottom bar. It sends cancel, waits for the plug-in, then puts the instruments in the safe state.
- **A value was entered on the wrong board or test:** enter the right value again (Enter twice replaces; the first stays in the journal), or skip with a reason.
