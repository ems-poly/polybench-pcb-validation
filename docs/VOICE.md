# Polybench voice: hands-free operation with a local LLM

Status: first version, HOST-ONLY (unit tests, `say` clips through real mlx-whisper, e2e 38/38 at 900 px; no live microphone run yet).
Living document, edited in place.

## 1. What it does

The operator speaks ("S1 0.2 volts", "all boards 2 ohms", "accept", "next test", "note R3 is 0603"). Polybench turns each
utterance into a **proposal** on the current test through the same path the function test and the instruments use
(`Engine.add_proposal`), answers by voice (macOS `say`), and journals the utterance as a `voice` event. Nothing is
accepted without "accept" (or the grace period for a clean PASS / record value); a FAIL or review value needs "accept fail".
Everything runs on this Mac: mlx-whisper for speech, Ollama for the optional LLM, `say` for the reply. No cloud calls.

## 2. Pipeline

    mic (sounddevice, server side) ── PTT (key / button) ──┐
                                   └─ hands-free VAD ──────┴─► gates ─► STT (mlx-whisper) ─► understand ─► controller ─► engine
                                                                       (speech segments only)  grammar first      proposals, accept, nav,
                                                                                               LLM fallback       notes, voice journal, TTS

## 3. Interfaces (build contract, 16 Oct)

Package `src/polybench/voice/`. Ported PoC modules keep their names.

### 3.1 `voice/understand.py` (NLU, pure: no engine, no I/O except the Ollama call)

```python
@dataclass
class BenchView:                    # what the operator is looking at; built by the controller from engine state
    step_id: str
    tests: list[dict]               # current step's tests in protocol order: {id, title, kind, unit, expected, options, limit}
    boards: list[str]               # boards the current step applies to and that are open (not gated, not stopped), e.g. ["S01", ...]
    all_boards: list[str]           # every board of the batch
    test_id: str | None             # current test (None on a step page without a test)
    board: str | None               # current board
    row_mode: bool = True           # batch (row) mode: a bare value goes to `board`, the controller then moves to the next open board
    settled: dict[str, bool] = {}   # board -> current test already has an accepted/skipped value or a pending proposal
    recent: list[str] = []          # last few entries as text, for the LLM context

def interpret(text: str, view: BenchView, llm: dict | None = None) -> dict
    # llm = {"enabled": bool, "model": str, "url": str, "timeout_s": float}; None or enabled False = grammar only
```

Result (always JSON-serialisable):

```python
{
  "transcript": str,
  "route": "grammar" | "llm" | "none",
  "model": str | None,              # the Ollama model when route == "llm"
  "intent": "values" | "accept" | "accept_fail" | "reject" | "cancel" | "skip" | "skip_board" | "undo"
            | "next_test" | "prev_test" | "goto_test" | "set_board" | "next_board" | "prev_board"
            | "note" | "which_test" | "progress" | "repeat" | "unclear",
  "values": [{"board": "S01", "test_id": "U01", "value": "0.2", "unit": "V", "heard": "0.2 volts"}],
                                    # value is a string engine.evaluate() accepts, already in the test's unit
                                    # (0.2 V on a mV test -> "200"); yn -> "yes"/"no"; choice -> option text; hex -> "0x.."
  "boards": ["S03"],                # for set_board / skip_board / note
  "test_id": "U02" | None,          # for goto_test, or the test the values/note refer to
  "text": str,                      # note text, skip reason
  "reason": str | None,             # why it is unclear / why a value was refused
  "grammar": {"ok": bool, "reason": str | None, "fields": dict | None},
  "llm": {"model": str, "ms": int, "ok": bool, "raw": dict | None, "reason": str | None} | None,
}
```

Rules: grammar first (the PoC's `Engine.simple` rule decides when the grammar is trusted alone); the LLM only for free
phrasing; LLM output goes through the PoC validator (`extract.resolve`): every number must be in the transcript and every
board must have been spoken. **An utterance without a number never yields a number value.** "all boards" = `view.boards`.
Board words: "S1", "S01", "S 1", "es one", "board three", "boards one to five", "boards 1 and 2", up to 40 boards.

### 3.2 `voice/controller.py` (runtime; owns state, applies to the engine)

`VoiceController(sess)` built by `server.Session`. Methods used by the routes:
`status() -> dict`, `set_config(patch) -> dict`, `set_context(step, test_id, board)`, `set_visible(bool)`,
`enable(bool)`, `handsfree(bool)`, `ptt_start()`, `ptt_beat()`, `ptt_stop()`, `ptt_cancel()`,
`handle_text(text, source="typed") -> dict` (one utterance, returns the transcript entry),
`feed_wav(path, via="ptt"|"vad") -> dict` (test and demo path: a WAV file through the gates, STT and the same pipeline),
`cancel_pending()` (Esc / "cancel": stops a grace auto-accept), `models() -> dict` (installed Ollama models).

Applying: values -> `engine.add_proposal(..., source={"kind": "voice", "key": "voice", "component": "voice", "route",
"model", "utterance": n, "transcript", "operator"})`; "accept" -> `engine.accept` on the last voice utterance's clean
proposals (verdict pass/none, no flags); a fail/review proposal is held and read back; "accept fail" -> `engine.accept(confirm=True)`;
"reject" -> `engine.reject`; navigation changes the controller's context and is broadcast to the page; "note" -> `engine.note`
linked to step, board, test and any BOM designators named in the text.

Grace: after a clean PASS or record proposal, the reply reads "<board> <value>. Say next test to accept, or reject." and, when `grace_s > 0`,
`grace_s` seconds after the reply the proposals are accepted (reply "Accepted.", the test stays) unless "reject", "cancel" or Esc came first.
"next test" accepts every pending clean value (all boards of an "all boards" entry) and moves on; its reply is only the next test read-out.
"accept" accepts and stays. "reject" or "cancel" discards the pending value (reply "Rejected.") and stays. A FAIL, review, flagged (outside
a guess/TBR limit) or stop-rule value is never accepted by "next test" or by the timer: the reply is "<value> is outside the limit: say accept
fail, or reject." and "next test" answers "Pending fail: say accept fail or reject first." and records nothing.

Range question (number tests with a min or max limit, not in AOI review): an utterance that is not understood is answered with "Is the
reading <limit, spoken> on <board>?" instead of the hint. Answers: "yes" (this board), "yes all boards" / "yes for all" (every applicable
board still empty), "yes S2" / "only S2" (named boards), "no" / "no S3" (voice asks "Say the reading for S3" and takes the next utterance as
that board's value), "cancel" (nothing recorded). Anything else drops the question. A confirmation is not a measurement: it is stored as
`value "IN RANGE"`, `origin "operator_confirmed"`, `num null`, journalled with `via "voice_range"` and the question text, takes the verdict of the
limit class (pass for hard and guess, none for tbr and record), counts as settled, and is ignored by the similarity highlight and the gate
medians. The cell shows "✓ in range (no value)" and the report lists it under "Confirmed in range, no reading". Tests without a numeric limit
(record class, text, yn) keep the hint.

### 3.3 Journal: event type `voice`

One per utterance, appended through `Engine._emit("voice", payload)`:
`{utt, source: ptt|handsfree|typed|clip, transcript, route, model, intent, values, boards, test_id, step, board,
outcome: applied|proposed|rejected|question|unclear, detail, reply, stt_ms, llm_ms, audio, bench?}`. A bench command adds `bench` (6.1); a guided-mode entry adds `guide: {section, n, total}` (6.2).
`State._on_voice` keeps the last 300 in `state.voice`; the snapshot carries them as `voice`. Old journals (no `voice`
events) replay unchanged. The proposals and accepts themselves are the normal `proposal` / `accept` events with
`source.kind == "voice"`.

### 3.4 HTTP (all JSON, 127.0.0.1)

| Route | Body | Returns |
|---|---|---|
| `GET /api/voice/status` | | `{guide: {section, title, board, n, total, step: {title, kind, state, text}, steps: [{n, title, state}], last, done, summary, hold: {left_s, window_s} or null} or null, bench: {confirm: {text, title, board, deadline_s} or null, last: {text, reply, outcome, ts} or null, running: {title, board, state} or null}, enabled, handsfree, listening, recording, speaking, muted_tts, level, in_speech, busy, stt: {ok, model, detail, install}, llm: {ok, model, models, detail, install}, tts: {ok, voice, rate, detail}, config, pending: {utt, deadline_s} or null, context}` |
| `POST /api/voice/config` | patch of the config | status |
| `GET /api/voice/models` | | `{ok, models: [names], default, detail}` |
| `GET /api/voice/voices` | | `{voices: [names]}` (`say -v ?`) |
| `POST /api/voice/enable` | `{on}` | status |
| `POST /api/voice/handsfree` | `{on}` | status |
| `POST /api/voice/visibility` | `{visible}` | status |
| `POST /api/voice/context` | `{step, test_id, board}` | `{ok}` |
| `POST /api/voice/ptt` | `{action: start|beat|stop|cancel}` | status |
| `POST /api/voice/text` | `{text}` | transcript entry |
| `POST /api/voice/clip` | `{path, via}` | transcript entry (only with `--simulate` or `POLYBENCH_VOICE_TEST=1`) |
| `POST /api/voice/cancel` | | status |
| `POST /api/voice/guide` | `{section, context}` | transcript entry (the section's Guide button: the same as saying "guide <section>") |

Server-sent events (existing `/api/events` stream): `{"kind": "voice", "entry": {...}}` per utterance,
`{"kind": "voice_nav", "step", "test_id", "board"}` when voice moves the cursor, `{"kind": "voice_status", ...}` on
enable/hands-free/speaking/pending changes, and the usual `{"kind": "state"}` after engine changes.

### 3.5 Config

`~/.config/polybench/voice.json` (env `POLYBENCH_VOICE_CONFIG` overrides the path; tests use a temp file). Missing file =
defaults. Keys: `llm_enabled` (true), `llm_model` ("qwen3:8b"; see §7), `ollama_url` must be loopback, `ollama_url` ("http://127.0.0.1:11434"),
`stt_model` ("mlx-community/whisper-large-v3-turbo"), `stt_backend` ("whisper" | "sidecar": `<clip>.txt` next to the WAV, for CI),
`tts_enabled` (true; the "Voice readouts" switch, false = nothing is spoken: the old "Mute replies" turned on), `tts_ack` (true; "Voice acknowledgements": accepted, rejected, cancelled and similar short replies are spoken), `tts_backend` ("say" | "log"), `tts_voice` (""), `tts_rate` (200), `grace_s` (3), `hf_gap_s` (0.8), `own_readout_tail_s` (0.5, TBR; UIR-37 half-duplex: hands-free input is ignored while a readout plays and this long after, and what was heard is journaled as "dropped: own readout"; push-to-talk is not gated, it stops the readout),
`ptt_key` ("Backquote"), `row_mode` (true), `input_device` (null), `confirm_simulated` (true), `confirm_timeout_s` (10, 3 to 60).

### 3.6 What is spoken (UIR-12)

One rule, in `voice/tts.py` `spoken_summary`, applied by `Tts.say` to everything that is spoken (value read-backs, guide, bench commands,
sequence announcements): text in brackets is dropped; a token longer than `LONG_VALUE_CHARS` (12, TBR) that holds a digit (hash, hex, chip ID,
serial) is spoken as "value on screen"; the result is cut to `MAX_SPOKEN_WORDS` (20, TBR), at a sentence end when one fits. The transcript, journal
and screen keep the full text.

## 4. Origin of the modules

| Earlier prototype module | Polybench | Status | What changed |
|---|---|---|---|
| `audio.py` | `voice/audio.py` | ported | unchanged |
| `vad.py` | `voice/vad.py` | ported | unchanged |
| `stt.py` | `voice/stt.py` | adapted | `build_prompt` uses Polybench test IDs, boards S01..Sn and the command words; the loader in `capture.py` sets `HF_HUB_OFFLINE` when the model is cached (no network) |
| `grammar.py` | `voice/grammar.py` | adapted | board words: S1, S01, S 1, S-1, "es one", board ten, "boards one to five", lists, up to 40 boards, "board S4"; B forms kept |
| `units.py`, `rowmode.py`, `extract.py`, `validate.py`, `llm.py`, `script.py`, `sheet.py` | same names | ported | header comment only |
| `session.py` | `voice/session.py` | ported, test and eval harness only | lazy `aoi` import; the live app uses the Polybench engine as the proposal store |
| `engine.py` (capture half) | `voice/capture.py` | adapted | PTT, 0.5 s heartbeat and 2 s watchdog, post-roll, VAD worker, no-speech and hallucination gates, mute while speaking kept; mic open only while voice is on and the page visible; sidecar STT for CI; no partial transcripts, no cue sound |
| `engine.py` (routing: `handle_text`, `_try_multi`, `simple`, `COMPLEX`) | `voice/understand.py` | adapted | the same grammar-first routing behind the `interpret(text, view)` contract; Polybench step adapter (tests as rows, S01.. as boards), unit conversion into the test's unit, Polybench intents; a board fused to its value by Whisper ("S20.2") is split against the batch |
| `engine.py` (`say`) | `voice/tts.py` | adapted | voice and rate, `log` backend, mute, `when_done` (grace timer starts after the reply) |
| `session.py` (commands, accept guard) | `voice/controller.py` | not ported (new) | applies intents to the Polybench engine: proposals, accept / accept fail, reject, skip, navigation, notes, read-back, grace, journal |
| `server.py` (Flask) | `server.py` routes `/api/voice/*` | not ported (new) | FastAPI routes and SSE in the existing server |
| `static/index.html` | `static/voice.js` | not ported (new) | Voice tab in the bottom panel |
| `aoi.py`, `probes.py`, `probemap.py`, `fusionlink.py`, `functest/` | | not ported | Polybench has its own AOI, probe card and Fusion bridge |
| tests `test_grammar`, `test_extract`, `test_rowmode` | `tests/private/voice/` | ported | imports and fixtures; the Flask accept-route asserts of the first LLM test dropped |
| test `test_handsfree` | `tests/private/voice/test_handsfree.py`, `test_capture.py` | ported / adapted | VAD tests kept; the real-recording test needs the PoC's local clips (dropped); engine-based tests rewritten on `Capture` |
| `scripts/eval_extract.py`, `bench_stt.py` | `scripts/voice_eval_extract.py`, `voice_bench_stt.py` | ported | same sets and scoring; the clips are read in place from the PoC (`PBVOICE_POC_AUDIO`) |

## 5. Measured (HOST-ONLY, this Mac, 16 Oct)

| What | Polybench port | PoC |
|---|---|---|
| STT + grammar on the 78 synthetic clips (`voice_bench_stt.py`) | 76 of 78 (97 %), 0.27 s median | 76 of 78 (97 %), 0.27 s |
| LLM extraction eval, main 40 (`voice_eval_extract.py`, gemma4:26b) | 134 of 134 cells, 39 of 40 cases exact (s19 adds an extra note cell: model drift) | 134 of 134, 40 of 40 |
| Held-out set | 51 of 51 cells, 17 of 17 | 51 of 51 |
| `say` clip "S1 0.2 volts" through `feed_wav` | 'S01 0.2 volts.' -> proposal S01 V01 0.2 V, 1.7 s with model load | |
| Free phrasing through the LLM | "put three point three volts on boards two through four but board five read zero point nine" -> S02..S04 3.3 V, S05 0.9 V, route LLM gemma4:26b | |

## 6. Command list

| Say | Does |
|---|---|
| "S1 0.2 volts", "board three two ohms", "es one 0.2 volts" | value proposed on that board for the current test |
| "0.2 volts" | value on the current board; in row mode the cursor then moves to the next open board |
| "all boards 2 ohms", "boards one to five 3.3 volts" | value on every open board of the test (applies, not gated, not stopped) |
| "accept", "yes", "confirm" (when no start is waiting for it, see 6.1) | accepts the clean (PASS or record, no flag) proposals of the last utterance; a FAIL or review is held and read back |
| "accept fail" | accepts held FAIL / review values (the deliberate FAIL accept) |
| "reject", "scratch that" | rejects the last utterance's proposals |
| "cancel" | stops a grace auto-accept, or a start that waits for "confirm"; the proposals stay pending |
| "skip" | moves on without recording (row mode: next board; else next test) |
| "skip S3 reworking" | recorded skip with the reason; without a reason nothing is recorded |
| "next test", "next", "previous", "back", "test U02" | moves the page's current test |
| "board S4", "next board", "previous board" | moves the current board (see 6.1 for the board command) |
| "note R3 is 0603" | journal note linked to step, test, board and the BOM designators named; right after a row-mode value it links the board just finished |
| "which test" | reads the test, probes and expected value |
| "read out the test method", "method" | reads the section instruction, probe plus / minus, expected value and stop rule |
| "highlight", "show in Fusion" | re-sends the test's highlight to Fusion (`Session.highlight_test` when present, else `Session.highlight` with the designators and probe points) |
| "progress" | "U01: 4 of 10 boards done, 6 left, next S05" |
| "repeat", "say again" | repeats the last reply |

### 6.1 Command card: bench commands (board, start, stop)

Hands-free control of the test you are on. Say these with the page on a step that has an action (the play button).

| Say | Does |
|---|---|
| "board S1", "board one", "board S01", "go to board 3" | selects that board (the batch's own label). An unknown board is answered "No board S9." and nothing changes |
| "start test", "run test", "run it", "start" | runs the action of the current step on the current board, the same call as the play button |
| "run first power", "start measure" | the action whose title, section or id contains those words, when the step has several |
| "confirm", "yes confirm" | after Polybench read the run back, within 10 s: starts it |
| "stop", "stop test", "abort", "cancel test" | aborts the running action. Always works, never asks to confirm |
| "next test", "previous test", "repeat", "next board" | move between rows, as before |

- **Stop is local and immediate.** The words are matched by a keyword grammar before the language model and before any other
  utterance is handled; while an action runs the model is not called at all, so nothing can make a stop wait. It is the
  abort of the Stop button: `{"t":"cancel"}` to the plugin, then the safe state in a fresh process. Polybench says
  "Stopping." at once and "Stopped." when the run has ended and the safe state is done. While an action runs, any utterance of
  up to six words that contains stop, abort or halt aborts it (a stop that was not meant is harmless). A bare "cancel" is not a
  stop. A stop with nothing running says "Nothing is running.".
- **A start is read back and needs "confirm".** "Run Read rails and current on S1, 12 volts. Say confirm." Only "confirm" or
  "yes confirm" within `confirm_timeout_s` (10) starts it. Any other words, "stop", Esc, the Cancel button or the timeout
  cancel the request ("Start cancelled." / "No confirmation. Nothing started."). A "confirm" after the timeout is refused
  and never taken as an accept. The pending request shows in the strip at the top with its countdown.
- **Which actions need it.** The plugin API has no flag for an action that energises a rail, flashes or drives a bus, so every
  action that is not simulated needs "confirm". In simulate mode it still does unless the setting `confirm_simulated` is off
  (Voice settings, "Confirm simulated starts"); an action that has an operator prompt in the protocol always does, and the
  prompt is read out with the request. The real, non-simulated path is UNVALIDATED (no instrument was used).
- **Every gate stays.** The step gate, the power interlock (a board on hold is refused and the reason is spoken: "Refused:
  step is gated: power interlock: ..."), a stop on the board, and one action at a time ("Read rails and current is still
  running. Say stop to abort."). Voice has no way around them.
- **Several actions.** "start test" runs the action of the test you are on; if that is not one action it names the options
  ("Say run first power or run measure"): say "run" and some words of the title.
- **Journal.** Every command, read-back, confirmation, refusal and timeout is a `voice` event (source `ptt`, `handsfree`,
  `typed` or `clip`; `timer` for a timeout, `system` for "Stopped."), with the transcript, the reply and a `bench` block
  `{cmd, board, step, action, run, confirm, refused, cancel_ms}`. The run itself is the usual `action_start` / `action_end` /
  `safe_state` events.
- **The microphone keeps listening during an action.** It is muted only while Polybench itself speaks and for a moment after
  (so it does not hear its own reply), so say "stop" after the reply, or at once if nothing is being said. Speech to text
  does not stream partial transcripts yet; `VoiceController.handle_partial` is ready for it.

Grace: with `grace_s > 0` a clean PASS / record proposal is accepted `grace_s` seconds after the reply has finished, unless
"cancel", "reject" or Esc came first; a strip at the top of the editor counts down. A new value said while a grace runs accepts
the earlier clean ones. Never for FAIL, review or a value outside a guess / TBR limit (those are read back as such).

## 6.2 Guided mode: a test section step by step

Say "guide me" (or "start section", "guide rails in band", or press a section's **Guide** button) and Polybench walks the
section for the current board, one numbered step at a time: it says what the step is, you say "ready", it runs it and reads
the result back. The steps come from the protocol; nothing in the protocol has to change.

| Say | Does |
|---|---|
| "guide me", "start section", "guide rails in band", "walk me through rails in band" | opens the walk of the current section, or the section whose title has those words, and announces step 1 |
| "ready", "go", "go ahead" | runs this step's action. Said after the announcement it is the spoken confirmation of that action (below) |
| a value ("5.02 volts") | on a manual step: fills that step's test, read back in the same words |
| "next step", "next" | takes the clean readings of this step and announces the next one. Refused until the step is done: "Step 1 is not done. Say ready, or skip step." |
| "previous step", "back", "repeat", "which step" | back one step, say this step again |
| "step two", "go to step 3" | jumps to that step |
| "redo", "redo step 1" | runs the step's action again, the same path as the per-test Redo button: the new value replaces the old one in the same attempt (the old one stays in the test's history); never a step retest. Also after a failure, a stop, or a lapsed hold |
| "skip step" | moves on without running or reading it; the section summary counts it as not read |
| "stop" | aborts the running action at once (6.1) and pauses the walk: "Stopped. Guide paused on step 2. Say ready to run it again." |
| "end guide" | closes the walk. An action still running keeps running: say stop |
| "next section" | after "Section done": opens the next section of the same step |

While a walk is open "ready", "next", "repeat" and "back" belong to the walk; with no walk open they keep their older meaning
(for example "next" is "next test", "confirm" is accept).

- **The walk.** The section's actions in their listed order (an action marked `advanced: true` is left out), then the tests no
  action proposes: those are the manual steps. If the section needs another order, give it: `guide: [{action: power_on}, {test:
  P02}, {action: read_5v_an}]` on the section (the linter checks that each entry is an action or test of that section). A step
  may carry a one-line spoken hint in the action's `say:` (else the section instructions' first sentence is used on step 1).
- **What it says.** Step 1: "Rails in band, board S1, three steps. Step 1 of 3: Turn on 12 V. This turns on 12 volts, 100
  milliamp limit. Say ready." The voltage and current limit come from the action's `volts` and `ilim_a` parameters, an operator
  prompt is read, and a manual step reads the test's `probe` ("Step 3 of 3: Measure Ripple on 5V. Probe plus on TP3. Say the
  value."). Each announcement is about twenty words.
- **"Ready" is the confirmation.** Outside the guide a start is read back and waits for "confirm" (6.1). In the guide the
  announcement already named what the step does (its title and, for an energising action, the volts and the current limit), so
  "ready" after it counts as the spoken confirmation: there is no second question. The runner still checks every gate. Say
  "stop" before "ready" to change your mind; nothing starts without the word.
- **Readings.** When the step ends, each reading is spoken with its unit and verdict, three significant figures: "5V rail:
  5.02 volts, pass." / "5V_AN rail: 5.2 volts, review, outside the guess band." / "5V rail: 4.5 volts, fail, below 4.85." A reading
  arrives either as a `proposal` event (a protocol that proposes instrument readings) or as an `accept` event with `auto: true`
  (a protocol that records them automatically); both are read once. Operator entries and voice values are not read as instrument
  readings. A table result is read as its size and verdict ("5V rail against load: 5 rows, 2 out of band, fail."), never as data. A failed action says its plain reason ("Power analyzer not reachable. Say redo to try again, or skip step.") and the
  walk stays on the step. After the last step: "Section done. 2 pass, 1 to review." and "Say next section".
- **"Next step" and the results.** An instrument reading is a result from the moment the action records it (`accept` with `auto: true`),
  whatever its verdict: "next step" has nothing to accept for it, emits no accept event and records nothing twice. A failing reading
  is already in the anomalies strip with its Redo, does not block the walk and counts under "to review". Values said by voice on a
  manual step stay proposals: the guide reads the value back, and "next step" accepts a clean one afterwards. A failing value you said
  yourself blocks "next step" until you say "accept fail" or "reject". Only a protocol that opts out (`record: propose`) leaves
  instrument readings pending; "next step" then takes the clean ones as before.
- **Hold window.** When an action leaves an output on (`leave: on` on `n6705b.first_power`, whose profile has a `watchdog_s`, or
  any action with `hold_s` in its parameters), the step ends with "Hold window 60 seconds.", at 15 seconds left it says "15
  seconds left on the hold.", and when the output has gone off by itself: "The hold lapsed and the output is off. Say redo step
  1." The panel counts it down.
- **Gates and safety are unchanged.** A gated, stopped or interlocked board is refused with the reason spoken and no walk opens;
  one action at a time ("Read 5V is still running. Say stop to abort."); stop is matched first by the local grammar (6.1).
  Changing the board ("board S2") restarts the walk for that board at step 1.
- **The page.** A compact panel at the top of the page shows "Step 2 of 3", the step and its state, the hold countdown, the last
  reading and the buttons Ready, Next, Repeat and End (the same words, keyboard reachable); the line under the talk bar reads
  "Guide: step 2 of 3". At 390 px the panel floats over the page.
- **Journal.** Every guided utterance and every spoken reading is a `voice` event with a `guide` block `{section, n, total}`
  (`system` source for the readings, the lapse and the hold warning). The runs are the usual `action_*` events.

## 7. Install and permissions

1. `.venv/bin/pip install -e '.[voice]'` (sounddevice, mlx-whisper). The Whisper model (about 1.6 GB) is already in the
   Hugging Face cache on this Mac; on another Mac run the line the Voice settings show once with network.
2. Ollama running with the default model: `ollama pull qwen3:8b` (not pulled on this Mac yet). Until it is pulled the best
   installed model runs (gemma4:26b here, the PoC's model) and Settings shows "Default model qwen3:8b not installed: run
   `ollama pull qwen3:8b`" with a Copy button. Without Ollama voice works grammar-only and says so.
3. macOS microphone permission for the terminal that runs `polybench` (System Settings > Privacy & Security > Microphone):
   the microphone opens in the server process the first time Voice is switched on.
4. Replies use `say`; choose the voice and rate in Settings, or mute them.

## 8. Known shortcuts

- No live-microphone run yet (the e2e feeds WAV files through the same gates and STT; the VAD is tested on synthetic streams).
- The LLM command enum is the PoC's; "read_method" and "highlight" are grammar-only phrases.
- Whisper merges some board/value pairs ("S20.2"); the split assumes the batch's board count.
- Hands-free shares the PoC's energy VAD: a second talker near the mic is not rejected.

## 9. Privacy: local only

Settings shows the line, for example: **"Voice: local only: STT mlx-community/whisper-large-v3-turbo (cached), LLM gemma4:26b at 127.0.0.1"**.

- STT: `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` are set when the voice package is imported, before huggingface_hub
  loads. Only models in the local Hugging Face cache load (here: whisper-large-v3-turbo, -medium, -small); a missing model is
  an install hint, never a download.
- LLM: `ollama_url` must be http on 127.0.0.1, localhost or ::1; anything else is refused by the config and again before every call.
- Page: no external requests. IBM Plex Sans and Mono are vendored in `static/vendor/fonts/` (SIL OFL 1.1, `OFL.txt`); the
  Google Fonts link is gone.
- `tests/test_no_external_urls.py` fails if any file under `src/` names a non-loopback http(s) URL (licence files excepted),
  if a non-loopback `ollama_url` is accepted, or if the offline flags are not set.

## 10. LLM model choice

Default **`qwen3:8b`** (Ollama library tag checked on ollama.com/library/qwen3/tags: 8B, 5.2 GB, 40K context): a 7-9B
instruction model so Polybench runs on lower-spec laptops; it follows a JSON schema well and supports
`think: false`, which `llm.py` sends. Alternatives in the same class: `gemma4:e4b` (gemma 4, about 8B total, 6.6 GB, closest to
the PoC family) and `llama3.1:8b`. Larger installed models stay selectable in Settings, which lists each with its parameter size.

| Model | Extraction eval (main 40) | Median LLM time |
|---|---|---|
| gemma4:26b (installed, PoC model) | 134 of 134 cells, 39 of 40 cases exact | 1.1 s |
| qwen3:8b | not run: not installed (`ollama pull qwen3:8b`) | |

The grammar handles the common commands and values without the LLM; the model only reads free phrasing.

## Revision history

| Date | Change |
|---|---|
| 2026-10-16 | First version: build contract (interfaces, journal event, routes, config) |
| 2026-10-16 | Port table, measurements, command list (incl. read out the test method, show in Fusion), install; grace entry `source: grace`, `pending.armed`, status `mic` and `nlu` blocks |
| 2026-10-16 | Privacy: offline STT, loopback-only Ollama, vendored fonts, URL test, privacy line; default LLM qwen3:8b with fallback to the best installed model, sizes in the picker |
| 2026-10-19 | Bench commands: board select, start with spoken confirm, stop that always works, "next test"; command card 6.1; config `confirm_simulated`, `confirm_timeout_s`; status `bench`; journal `bench` block |
| 2026-10-19 | Guided mode: "guide me" walks a section step by step (6.2), `guide:` section key, "ready" as the spoken confirmation, readings read back from `proposal` and auto `accept` events, hold-window warnings; status `guide`, route `/api/voice/guide`, journal `guide` block |
