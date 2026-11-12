# Ported from the earlier voice prototype prg/llm.py. Changes for Polybench: none (prompt and schema unchanged).
"""Local LLM (Ollama) extraction: the transcript plus the sheet as context in, a list of cell updates out.

The model never writes to the sheet. Its JSON is validated cell by cell in `extract.py`; this module only
builds the prompt, calls Ollama (temperature 0, fixed seed, JSON schema constrained) and returns the raw answer.
The static part (rules, examples, the sheet's rows) comes first so Ollama's prompt cache reuses it between calls;
only the cursor, the entries so far and the transcript change.
"""
from __future__ import annotations

import json
import urllib.request

URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "gemma4:26b"
SEED = 42

UNIT_NAMES = ["ohm", "kohm", "Mohm", "mohm", "V", "mV", "uV", "A", "mA", "uA", "Hz", "kHz", "MHz", "s", "ms", "us", "percent"]
UNIT_FROM_LLM = {"ohm": "Ω", "kohm": "kΩ", "mohm": "mΩ", "Mohm": "MΩ", "V": "V", "mV": "mV", "uV": "µV", "A": "A", "mA": "mA",
                 "uA": "µA", "Hz": "Hz", "kHz": "kHz", "MHz": "MHz", "s": "s", "ms": "ms", "us": "µs", "percent": "%"}
COMMANDS = ["none", "accept", "reject", "undo", "skip", "next", "previous", "repeat"]

SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "enum": COMMANDS},
        "updates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "boards": {"type": "array", "items": {"type": "integer"}},
                    "kind": {"type": "string", "enum": ["number", "ol", "ok", "yes", "no", "text", "skip", "none"]},
                    "number": {"type": "string"},
                    "unit": {"type": "string", "enum": UNIT_NAMES + [""]},
                    "unit_heard": {"type": "string"},
                    "text": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["pass", "fail", "none"]},
                    "note": {"type": "string"},
                },
                "required": ["id", "boards", "kind", "verdict"],
            },
        },
    },
    "required": ["command", "updates"],
}

RULES = """You fill in a bench test sheet from a technician's speech transcript. The transcript comes from Whisper, so words are often misheard (examples: "megahertz" for megaohm, "milliamps" for milliohms, "ports" for boards, "open load" for OL, "see oh five" for C05). You get the sheet's rows and what is already filled in. Answer ONLY with JSON: {"command": ..., "updates": [...]}.

Each update fills cells of ONE row. Fields:
- id: the row ID, copied exactly from the sheet. If the technician names no row, use the current row.
- boards: list of board numbers (integers) the update applies to. Expand "all boards", "all five", "every board" to the full list; "all apart from B3" / "all except B3" to every board except 3 (and give B3 its own update if a value is stated for it); "B1 to B4" to [1,2,3,4]. Boards the technician did not mention must not appear. If no board is spoken, give boards [] and the program assigns the current board (a list of values with no boards, "3.29 3.30 3.31", is one update per value, each with boards []). For a verdict that is meant for the whole row, boards may be [].
- kind: number (a measured value), ol (over range / open load / OL), ok, yes, no, text (a finding in words, only for rows whose unit is OK/text), skip (the technician skips those boards, "skip B3, reworking": boards = the skipped ones, note = the reason), none (only a verdict or a note).
- number: the digits only, as a plain decimal string, EXACTLY as spoken ("point seven five" is "0.75", "four point seven" is "4.7"). Never convert units and never compute. For "same as before" repeat the earlier number.
- unit: the unit the technician spoke, resolved against the row. If the spoken unit cannot be a unit of that row's dimension, use the row's unit with the same prefix (a "megahertz" reading on an ohm row is Mohm; "milliamps" on an ohm row is mohm) and put the misheard word in unit_heard. Leave unit "" when none was spoken.
- unit_heard: the unit word as it appears in the transcript, or "".
- text: for kind text, a short phrase from the transcript; otherwise "".
- verdict: pass only if the technician says pass / passed, fail only if they say fail / failed (or a clear equivalent such as defective), for these boards; otherwise none. A value or an OK / cleared / open load alone is not a verdict. Do not judge values yourself; the program compares measured values with the Expected column.
- note: a finding or explanation that is not a measurement, as a short sentence in the technician's words ("traced a short on the power IC at the back"); otherwise "". Only a defect, cause or observation goes here; never restate the result ("passed", "cleared", "open load"), never repeat the value or text you already gave, and never comment that a value is below or above the limit.

Rules:
- One update per group of boards that share the same value. Different values for different boards are separate updates.
- A transcript can touch several rows; give each row its own updates.
- Never invent a value, a board or a row. If something is not in the transcript, leave it out. If nothing in the transcript is a result, a note or a command, answer {"command": "none", "updates": []}.
- command is for pure commands (accept, reject, undo, skip, next, previous, repeat) and then updates is []. Otherwise "none".
- A correction ("no, B2 was 1.2") is a normal update for that board with the new value.
- "Pass" or "passed" alone applies to the current row. "Fail boards two and four" is verdict fail with boards [2,4].
- Y/N rows (unit Y/N) take kind yes or no, even when the technician says passed, cleared or OK. OK/text rows take kind ok or text.

Examples (a made-up sheet; row Q1 is Ω "Expected: high", row Q2 is V "Expected: OL", row Q3 is Y/N, row Q4 is OK/text):
"Q1 all boards four point seven k" -> {"command":"none","updates":[{"id":"Q1","boards":[1,2,3,4,5],"kind":"number","number":"4.7","unit":"kohm","unit_heard":"k","text":"","verdict":"none","note":""}]}
"Q2 open load on one two and five, board three read point six volts, pass" -> {"command":"none","updates":[{"id":"Q2","boards":[1,2,5],"kind":"ol","number":"","unit":"","unit_heard":"","text":"","verdict":"none","note":""},{"id":"Q2","boards":[3],"kind":"number","number":"0.6","unit":"V","unit_heard":"volts","text":"","verdict":"none","note":""},{"id":"Q2","boards":[],"kind":"none","number":"","unit":"","unit_heard":"","text":"","verdict":"pass","note":""}]}
"Q3 yes on every board except B2 which is no, it was missing" -> {"command":"none","updates":[{"id":"Q3","boards":[1,3,4,5],"kind":"yes","number":"","unit":"","unit_heard":"","text":"","verdict":"none","note":""},{"id":"Q3","boards":[2],"kind":"no","number":"","unit":"","unit_heard":"","text":"","verdict":"none","note":"it was missing"}]}
"Q4 board four looks fine" -> {"command":"none","updates":[{"id":"Q4","boards":[4],"kind":"ok","number":"","unit":"","unit_heard":"","text":"","verdict":"none","note":""}]}
"Q1 about two megahertz on board two" -> {"command":"none","updates":[{"id":"Q1","boards":[2],"kind":"number","number":"2","unit":"Mohm","unit_heard":"megahertz","text":"","verdict":"none","note":""}]}
"next one please" -> {"command":"next","updates":[]}
"""


def available(url: str = URL, timeout: float = 1.5) -> bool:
    try:
        urllib.request.urlopen(url + "/api/tags", timeout=timeout).read()
        return True
    except Exception:
        return False


def _short(s: str, n: int) -> str:
    s = (s or "").replace("`", "").replace("|", "/").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def sheet_table(script) -> str:
    """The static part: one line per row, ID | check | unit | expected."""
    lines = ["Sheet rows (ID | Check | Unit | Expected):"]
    for r in script.rows:
        lines.append(f"{r['id']} | {_short(r['label'], 70)} | {_short(r['unit'], 12)} | {_short(r['expected'], 110)}")
    cols = sorted({c["kind"] for c in script.cells})
    has_notes = "notes" in cols
    lines.append(f"Boards on the sheet: {', '.join(script.boards)}. Pass/fail column: {'yes' if 'passfail' in cols else 'no'}. "
                 f"Notes column: {'yes' if has_notes else 'no'}.")
    return "\n".join(lines)


def system_prompt(script) -> str:
    return RULES + "\n" + sheet_table(script)


def user_prompt(transcript: str, state: dict) -> str:
    """The dynamic part. `state` = {row_id, board, filled: [str], recent: [str]}."""
    filled = "; ".join(state["filled"]) if state.get("filled") else "nothing yet"
    recent = "; ".join(state["recent"]) if state.get("recent") else "none"
    batch = (f" Batch mode: a test is read across the boards in order; a value with no board goes to the next board ({state['board']} first).\n"
             if state.get("batch") else "\n")
    return (f"Current row: {state.get('row_id') or 'none'}. Current board: {state.get('board') or 'none'}.{batch}"
            f"Filled or proposed in the current row: {state.get('row_filled') or 'nothing yet'}.\n"
            f"Last entries (newest last): {recent}.\n"
            f"Transcript: {transcript}")


def prompt_summary(script, state: dict, model: str = DEFAULT_MODEL) -> str:
    """One line for the trace panel: what the model was told."""
    return (f"{model}, JSON schema (list of cell updates), temperature 0, seed {SEED}; context: {len(script.rows)} sheet rows "
            f"(ID, check, unit, expected), boards {', '.join(script.boards)}, current row {state.get('row_id') or 'none'}, "
            f"current board {state.get('board') or 'none'}, row so far: {state.get('row_filled') or 'nothing'}, "
            f"{len(state.get('recent') or [])} recent entries")


def extract(transcript: str, script, state: dict, model: str = DEFAULT_MODEL, url: str = URL, timeout: float = 60.0) -> dict:
    body = {"model": model, "stream": False, "think": False, "format": SCHEMA, "keep_alive": "30m",
            "options": {"temperature": 0, "seed": SEED, "num_predict": 700},
            "messages": [{"role": "system", "content": system_prompt(script)},
                         {"role": "user", "content": user_prompt(transcript, state)}]}
    req = urllib.request.Request(url + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return json.loads(data["message"]["content"])
