# Live multimeter (T6.8): UNI-T UT161E over the CH9329 cable

Status: first version, 27 Oct 2026. Code: `src/polybench/meter/`, `src/polybench/plugins/dmm.py`, card `src/polybench/static/meter.js`. Tests: `tests/test_meter_*.py`, `tests/plugins/test_dmm_read.py`, walk `tests/e2e/meter_walk.py`. Living document, edited in place (revision table at the end).

The operator probes a board with the meter. The reading shows live in a card on the test page, is proposed into the test in focus, and the operator accepts it like any proposal. With auto-accept on, a stable proposal is accepted after a short delay, spoken, and can be undone.

## Setup

1. `pip install -e '.[dmm]'` (adds the `hidapi` package, `import hid`). The macOS wheel carries its own hidapi (it links only system frameworks): `brew install hidapi` is not needed. Without the extra the app runs and the card says how to install it.
2. Plug in the CH9329 cable (USB `1A86:E429`, product `WCH UART TO KB-MS_V1.8`; no serial port appears on macOS, it is a HID device) and switch the meter on. The meter's auto power-off and its USB function can leave the cable silent: the card then says "the cable is there but the meter does not answer".
3. Start Polybench as usual. The server opens the cable and keeps it (one owner of the HID handle). `--no-meter` or `POLYBENCH_METER=off` turns the reader off. `--simulate` uses a fake meter that replays scripted frames, so the whole flow runs with no hardware (`POST /api/meter/sim {"scene": "ohm"}` picks the scene: `dc_v dc_mv ohm ohm_ol hold ac_v freq cap ma unplugged plugged`).
4. `polybench doctor` shows a `multimeter` line: hidapi, cable found, model string (one Get Name). Absent is INFO, not an error, unless the protocol uses `dmm.*`.

Only two command bytes are ever sent to the meter, Get Name `0x5F` and Get Measurement `0x5E`; the code refuses to build any other (the others press the meter's buttons). One cable, one process: close other programs that hold it.

## What is proposed, and what is not

A reading is proposed only when all of these hold; otherwise the card still shows the number and says why in plain words.

| Not proposed when | Card says |
|---|---|
| overload (display has `O` and `L`; `-` makes it negative overload) | `OL` / `-OL`, "overload ... never proposed". Never converted to a number. |
| HOLD, REL, MIN, MAX, P-MIN or P-MAX is set | "HOLD is on: the display holds a stored or relative value" |
| AC, AC+DC, LPF, LoZ, clamp modes, or the AC flag | "AC and AC+DC readings are shown, not proposed" (the plan has no AC units yet) |
| the mode has no unit in the plan vocabulary (capacitance, hFE, live, NCV, degF) | "... has no unit in the plan vocabulary" |
| the (mode, range) pair is not in the model table (all of frequency today) | "range n of ... is not in the UT161E table" |
| no test in focus, or the focused test is not a `number` test | "no test in focus ..." / "takes a yn result, not a measured number" |
| quantity mismatch (meter on resistance, test wants V) | "the meter is measuring resistance (kΩ); this test wants voltage (V)" |
| not stable yet | "waiting for a stable reading (3/5)" |
| voice push-to-talk held, or an utterance being handled | "voice is active: the meter waits" |

Proposable modes (plan unit): DC V `02` V, DC mV `03` mV, resistance `06` and continuity `07` Ohm/kOhm/MOhm, diode `08` V, duty `05` %, temperature `0A` degC, DC uA `0C` uA, DC mA `0E` mA, DC A `10` A. Frequency `04` is parsed and shown but not proposed until its range ladder is captured (see readiness). The value is converted to the focused test's unit inside the same quantity (V mV uV, Ohm kOhm MOhm, A mA uA, Hz kHz MHz), exactly (decimal arithmetic).

**One proposal per settled reading.** After a proposal the meter is latched: the same settled value is not proposed again, in particular not into the next test after focus moves while the probe is still on the same point. It re-arms when the display shows overload or a non-number, the mode or range changes, a flag appears, or a stable value differs from the latched one by more than the stability band.

## Stability rule (TBR)

A reading is stable when the last `n` = 5 consecutive valid readings lie within ±0.2 % of their mean, with a floor of ±1 count of the last displayed digit. Overload, a non-number, a flag, or a change of mode or range empties the window. Values are configurable (env `POLYBENCH_METER_N`, `POLYBENCH_METER_PCT`, or `POST /api/meter/config`); they are TBR and are closed by a timed bench session with real probing.

## Proposal path

The reader thread proposes through `Engine.add_proposal` with source `{kind: meter, key: meter, component: <model>}` (never an operator-only route; the assistant is refused every `/api/meter` write). The focus is the one the page posts to `/api/ai/focus` (`hub.focus`), the same the AI connector reads. Evidence: model, mode and code, range index, display text, flag bytes, raw frame hex (`raw`). The engine computes the verdict from the plan's limits; the plug-in never does. A new stable value replaces the meter's pending proposal on that test (the engine keeps one pending proposal per source key); both stay in the journal.

## Auto-accept

Switch on the meter card, **off at every start**. When on: a stable proposal waits `delay` = 1 s (TBR), then `Engine.accept(..., via="meter-auto")`, the value is spoken through the voice controller's `say` ("five point zero zero seven volts, accepted"; spoken only while voice acknowledgements are on), and the page moves to the next test with the existing `advance()` (a `meter_advance` event). **Undo** stays on the card for `undo` = 5 s (TBR): it writes an `undo` journal event naming the accept it takes back (the accept stays in the journal; replay empties the cell), puts the test back in focus and does not propose the same reading again. Never auto-accepted, left as a proposal for the operator: a FAIL or REVIEW verdict (auto-accept never confirms), a gated or stopped board, a cell that already holds another value. A value that tripped a stop rule is accepted by hand only and has no Undo. The reading moving, auto-accept being switched off, a manual accept or reject, or voice becoming active during the delay cancels it.

## Voice

While push-to-talk is held, the operator is speaking or an utterance is being handled (`VoiceController.meter_blocked()`: `capture.recording`, `capture.busy` or `capture.seg.active`), the meter proposes nothing and a pending auto-accept is dropped when it fires.

## Connection

`not installed` (no hidapi), `no cable`, `silent` (cable but no answer), `connected`, `lost`. Unplugging shows `lost` within 1.5 s and proposes nothing; plugging back reconnects without a restart. Every failure becomes a state and a line on the card; none reaches the server log as a traceback.

## `dmm.read` action

`dmm.read` (entry point `polybench.actions`): waits up to `timeout_s` (10) for a stable reading, converts to `unit`, proposes through `map: {value: <test_id>}`. See [PLUGINS.md](PLUGINS.md). **Device ownership (decision):** one owner of the HID handle. When the server runs the action, it first **pauses the live reader** (which closes the cable) and resumes it when the run ends (`runner.py`); the child then opens the cable, sends only Get Name and Get Measurement, and closes it in `finally`. Run from a terminal while a server holds the cable, it fails with a plain message. Like every instrument action, its value is recorded as the result at once unless the protocol says `record: propose`.

## Sources of every protocol fact

| Fact | Source |
|---|---|
| Cable ids, product string, 64-byte reports, no serial port | our ioreg and hidapi enumeration, 27 Oct |
| UART fixed at 9600 baud, no set-up | WCH CH9329 datasheet; our bench run |
| Output report: `00`, count, bytes, zero padded to 65 | our bench run |
| Input report: `<len> <bytes>`, stale bytes after them, optional leading `00` | our captures (reports carry junk after the count; the count is authoritative) |
| Frame `AB CD len payload sum16`, checksum rule | our captures, every checksum verified |
| Command frames `AB CD 03 5F 01 DA`, `AB CD 03 5E 01 D9` | the brief's fact sheet; our bench run |
| Get Name: acknowledge frame then ASCII model | our capture (`UT161E`) |
| Measurement layout (mode, range, 7-char display, bar, 3 flag bytes) | our captures; consistent with public descriptions of the family protocol (facts only) |
| Flag bits, mode codes | public protocol descriptions (facts only, as written in the brief); `02` and `06` confirmed on our meter |
| UT161E ranges | UNI-T UT161 series user manual, specification tables (DC V 220.00 mV, 2.2000 V, 22.000 V, 220.00 V, 1000.0 V; resistance 220.00 Ohm to 220.00 MOhm, 7 ranges; DC uA 220.00 / 2200.0; DC mA 22.000 / 220.00; DC A 20.000) |
| Rate 13.4 readings/s | our bench run; re-measured 13.3/s by the product's reader (12 s, 161 readings, 0 bad frames) |

No code, docs or test fixtures of `antoinecellerier/dmm-tools`, `ljakob/unit_ut61eplus` or `mwuertinger/ut61ep` (all GPL) were opened, read, searched or fetched. The code ships under the repo's `LICENSE`.

## Readiness per mode (UT161E)

| Item | State |
|---|---|
| Get Name, model string `UT161E`, frame check, 13.3 readings/s, 0 bad frames, Ohm range 6 `OL.` shown and not proposed, live card on the real meter, `dmm.read` pausing the live reader and holding the cable once | HARDWARE-VALIDATED (27 Oct session, dial on Ohm, open leads) |
| DC V range 1 (5.007 / 5.008 V, unit V) | HARDWARE-VALIDATED (27 Oct capture; frames in the tests) |
| Every other (mode, range) unit in the table, and any numeric reading on Ohm | UNVALIDATED (manual only; marked `manual` on the card) |
| Frequency: range ladder unknown, nothing proposed | UNVALIDATED |
| Negative overload `-OL.` on this model | UNVALIDATED (handled by rule, never seen) |
| Flags REL HOLD MIN MAX P-MIN P-MAX and low battery on this model | UNVALIDATED (bit positions from public descriptions; needs a button press) |
| Unplug and reconnect | HOST-ONLY (fake device); live unplug UNVALIDATED |
| Stability rule, delay and undo values | TBR, HOST-ONLY; closed by a timed bench session |
| Auto-accept, spoken read-back with a real voice, Undo | HOST-ONLY (the voice backend is a log in tests; nothing was spoken) |
| UI card | HOST-ONLY (WebKit screenshots on the simulator, plus one on the real meter showing OL) |

## Out of scope (registered, not built)

The CP2110 cable (`10C4:EA80`), other UNI-T models (the table in `meter/models.py` is per model and takes another), AC readings (needs AC units in the plan format), logging meter streams to a file, any meter button command.

## Revision

| Rev | Date | Change | By |
|---|---|---|---|
| 1 | 2026-10-27 | First version: reader, card, proposal path, auto-accept, `dmm.read`, doctor, simulator | dmm-live |
