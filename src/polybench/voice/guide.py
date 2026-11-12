"""Guided section walk: "guide me" reads a section's steps one at a time, runs each on "ready" and reads the result back
(docs/VOICE.md, "Guided mode").

Two halves, like benchcmd.py. The functions at the top are pure: a protocol section in, a list of numbered steps or a sentence out.
`GuideCommands` is a mixin for VoiceController: it keeps the one walk, turns the spoken commands into the calls the UI makes (the
runner's start, the engine's accept) and listens to the engine's journal events to read the instrument's readings back.

Rules kept here:
- The walk is derived from the protocol: the section's actions in their listed order, then the tests no action proposes (manual
  readings). An optional section key `guide: [{action: id} | {test: id}, ...]` fixes the order and the contents instead.
- "ready" after a step's announcement is the spoken confirmation of that step's action. The announcement names the action and, when
  the action energises, the voltage and current limit. Outside the guide, "start test" then "confirm" stays as it was (benchcmd.py).
- No gate is bypassed: the engine's step gate, power interlock and stops (`Engine._check_open`) and the runner's one action at a time
  are checked exactly as for "start test". STOP is benchcmd's local grammar and acts before this module is consulted; it pauses the walk.
- Nothing here accepts a failing or reviewable result. Instrument readings are recorded at once by the action (auto accept); "next step" only takes clean voice values and the pending readings of a `record: propose` protocol, as "next test" does.
"""
from __future__ import annotations

import queue
import re
import threading
import time

from ..engine import CmdError
from .benchcmd import spoken_name, tokens, words_of

NUM_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"]
WORD_NUM = {w: i for i, w in enumerate(NUM_WORDS)}
WORD_NUM.update({"to": 2, "too": 2, "for": 4})
FILL = {"please", "okay", "uh", "um", "hey", "the", "and", "now"}
SECTION_NOISE = {"me", "through", "section", "this", "a", "my", "walk", "guide", "start", "begin", "test", "tests"}
READY_WORDS = {"ready", "go", "ahead", "im", "i", "am", "we", "are", "yes", "okay"}
HOLD_WARN_S = 15.0
DETAIL_WORDS = 16
VERDICT_BAND = {"guess": "the guess band", "tbr": "the TBR band"}


# --------------------------------------------------------------------------------------------- pure: the walk

def derive_walk(sec: dict) -> list[dict]:
    """The numbered steps of a section. Each step: {n, kind: action | manual, id, title, action: dict | None, tests: [ids]}."""
    acts = {a["id"]: a for a in sec.get("actions", []) if isinstance(a, dict) and "id" in a}
    tests = {t["id"]: t for t in sec.get("tests", []) if isinstance(t, dict) and "id" in t}

    def a_step(a: dict) -> dict:
        return {"kind": "action", "id": a["id"], "title": a.get("title") or a["id"], "action": a,
                "tests": [t for t in (a.get("proposes") or []) if t in tests]}

    def t_step(t: dict) -> dict:
        return {"kind": "manual", "id": t["id"], "title": t.get("title") or t["id"], "action": None, "tests": [t["id"]]}

    out: list[dict] = []
    order = sec.get("guide")
    if isinstance(order, list) and order:
        for e in order:
            if isinstance(e, dict) and e.get("action") in acts:
                out.append(a_step(acts[e["action"]]))
            elif isinstance(e, dict) and e.get("test") in tests:
                out.append(t_step(tests[e["test"]]))
    else:
        proposed: set[str] = set()
        for a in acts.values():
            proposed.update(a.get("proposes") or [])
            if not a.get("advanced"):
                out.append(a_step(a))
        out += [t_step(t) for t in tests.values() if t["id"] not in proposed]
    for i, s in enumerate(out, 1):
        s["n"] = i
    return out


def number_word(n: int) -> str:
    return NUM_WORDS[n] if 0 <= n < len(NUM_WORDS) else str(n)


def first_sentence(text: str, max_words: int = DETAIL_WORDS) -> str:
    """The first sentence of an instruction, plain and short for the ear."""
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text or "")
    t = re.sub(r"[*_`#>]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return ""
    t = re.split(r"(?<=[.!?])\s", t, maxsplit=1)[0].rstrip(".!? ")
    w = t.split()
    if len(w) > max_words:
        t = " ".join(w[:max_words]).rstrip(",;:")
    return t + "."


def _g(x) -> str:
    return f"{x:g}"


def _num(params: dict, key: str):
    v = params.get(key)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def is_energising(act: dict) -> bool:
    p = act.get("params") or {}
    return _num(p, "volts") is not None or str(act.get("plugin", "")).endswith(".first_power")


def energise_phrase(act: dict) -> str:
    """"This turns on 12 volts, 100 milliamp limit." The current limit is the action's own ilim_a (the profile may only lower it)."""
    p = act.get("params") or {}
    v, i = _num(p, "volts"), _num(p, "ilim_a")
    if v is None:
        return ""
    s = f"This turns on {_g(v)} volts"
    if i is not None:
        s += f", {_g(round(i * 1000, 3))} milliamp limit"
    return s + "."


def hold_s(act: dict | None) -> float | None:
    """Seconds an output stays on after the action returns: the action's `hold_s` param, else, for `leave: on`, the watchdog of the
    profile the action names. None when nothing is left on, or when the profile cannot be read (then no number is announced)."""
    if not act:
        return None
    p = act.get("params") or {}
    if _num(p, "hold_s") is not None:
        return float(p["hold_s"]) if p["hold_s"] > 0 else None
    if p.get("leave") != "on" or not p.get("profile"):
        return None
    try:
        from polystage_bench import profiles
        w = profiles.Profile.load(str(p["profile"])).watchdog_s
        return float(w) if w else None
    except Exception:
        return None


def reason_phrase(text: str) -> str:
    """The plain reason of a failed action: the first sentence, without a trailing system message."""
    t = re.sub(r"\s+", " ", text or "").strip()
    if not t:
        return "The action failed"
    t = re.sub(r"^(?:[A-Za-z_.]*(?:Error|Exception)): ", "", t)      # "RuntimeError: ..." from a plugin that raised
    t = t.split(". ")[0].rstrip(".")
    head, sep, tail = t.partition(": ")
    if sep and (len(t.split()) > 12 or re.search(r"Errno|\[|Traceback|\(|0x", tail)):
        t = head
    w = t.split()
    if len(w) > 14:
        t = " ".join(w[:14])
    return t[0].upper() + t[1:]


def sig3(x) -> str:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return str(x)
    if f == 0:
        return "0"
    s = f"{float(f'{f:.3g}'):f}".rstrip("0").rstrip(".")
    return s if "e" not in s else f"{f:.3g}"


def short_name(title: str, max_words: int = 5) -> str:
    w = spoken_name(title).split()
    return " ".join(w[:max_words])


def reading_phrase(test: dict, p: dict, unit_words=lambda u: u or "") -> str:
    """One reading as it is spoken: "5V rail: 5.02 volts, pass." p is a proposal or accept payload (value, unit, verdict, flags)."""
    name = short_name(test.get("title") or test.get("id", ""))
    val = p.get("value")
    if str(val).upper() == "IN RANGE":
        body = "in range"
    elif test.get("kind") == "table" and isinstance(val, dict):
        n = len(val.get("rows") or [])
        outs = sum(1 for e in (p.get("row_verdicts") or []) if e.get("out"))
        body = f"{n} {'row' if n == 1 else 'rows'}" + (f", {outs} out of band" if outs else "")
    elif test.get("kind") == "number":
        u = unit_words(p.get("unit") or test.get("unit"))
        body = f"{sig3(val)} {u}".strip()
    else:
        body = str(val)
    v, flags = p.get("verdict"), list(p.get("flags") or [])
    lim = test.get("limit") or {}
    if v == "pass":
        tail = ", pass"
    elif v == "review":
        tail = ", review" + (", outside " + VERDICT_BAND["guess"] if "outside_guess" in flags else "")
    elif v == "fail":
        tail = ", fail"
        try:
            n = float(val)
            if lim.get("min") is not None and n < lim["min"]:
                tail += f", below {sig3(lim['min'])}"
            elif lim.get("max") is not None and n > lim["max"]:
                tail += f", above {sig3(lim['max'])}"
        except (TypeError, ValueError):
            pass
    elif "outside_tbr" in flags:
        tail = ", outside " + VERDICT_BAND["tbr"]
    elif v == "invalid":
        tail = ", not valid"
    else:
        tail = ", recorded"
    return f"{name}: {body}{tail}."


# --------------------------------------------------------------------------------------------- pure: the grammar

def _number_of(toks: list[str]) -> int | None:
    for t in toks:
        if t.isdigit():
            return int(t)
        if t in WORD_NUM:
            return WORD_NUM[t]
    return None


def parse_guide(toks: list[str], active: bool) -> dict | None:
    """{"cmd": start | ready | next | prev | repeat | redo | skip | end | next_section | where | goto, "words": [...], "n": int | None}
    or None. Start phrases are always recognised; the others only while a walk is open (otherwise "next", "repeat" keep their meaning)."""
    core = [t for t in toks if t not in FILL]
    if not core:
        return None
    if core[0] == "guide" and (len(core) == 1 or "guide" not in core[1:]) and not any(t in ("end", "stop", "exit") for t in core):
        return {"cmd": "start", "words": [t for t in core[1:] if t not in SECTION_NOISE], "n": None}
    if core[0] == "walk" and core[1:3] == ["me", "through"]:
        return {"cmd": "start", "words": [t for t in core[3:] if t not in SECTION_NOISE], "n": None}
    if core[0] in ("start", "begin") and len(core) > 1 and core[1] == "section":
        return {"cmd": "start", "words": [t for t in core[2:] if t not in SECTION_NOISE], "n": None}
    if core[:2] == ["next", "section"] and len(core) == 2:
        return {"cmd": "next_section", "words": [], "n": None}
    if not active:
        return None
    if core[0] in ("end", "exit", "finish", "quit", "leave", "cancel", "stop") and "guide" in core and len(core) <= 3:
        return {"cmd": "end", "words": [], "n": None}
    if core in (["end", "section"], ["finish", "section"]):
        return {"cmd": "end", "words": [], "n": None}
    if set(core) <= READY_WORDS and ("ready" in core or core[0] == "go"):
        return {"cmd": "ready", "words": [], "n": None}
    if core in (["next"], ["next", "step"], ["continue"], ["move", "on"], ["next", "one"]):
        return {"cmd": "next", "words": [], "n": None}
    if core in (["previous"], ["previous", "step"], ["back"], ["go", "back"], ["last", "step"], ["back", "one"]):
        return {"cmd": "prev", "words": [], "n": None}
    if core in (["repeat"], ["repeat", "that"], ["repeat", "step"], ["say", "again"], ["again"], ["say", "that", "again"]):
        return {"cmd": "repeat", "words": [], "n": None}
    if core[0] in ("redo", "rerun", "retry", "re") or core[:2] == ["run", "again"]:
        rest = [t for t in core[1:] if t not in ("step", "it", "that", "again", "run")]
        n = _number_of(rest)
        if rest and n is None:
            return None
        return {"cmd": "redo", "words": [], "n": n}
    if core[0] == "skip" and len(core) <= 3 and "step" in core:
        return {"cmd": "skip", "words": [], "n": None}
    if core in (["which", "step"], ["what", "step"], ["where", "am", "i"], ["what", "now"]):
        return {"cmd": "where", "words": [], "n": None}
    if "step" in core and len(core) <= 4 and core[0] in ("step", "go", "goto", "to"):
        n = _number_of(core)
        if n is not None:
            return {"cmd": "goto", "words": [], "n": n}
    return None


# --------------------------------------------------------------------------------------------- the mixin

class GuideCommands:
    """Mixed into VoiceController (self.eng, self.sess, self.ctx, self.cfg, self.tts, self._lock, self._record, self._publish,
    self._changed and the BenchCommands methods are its)."""

    def _guide_init(self):
        self._g: dict | None = None                 # the open walk
        self._gq: queue.Queue = queue.Queue()
        self._gworker: threading.Thread | None = None
        self._gnow = time.monotonic                 # replaceable clock (tests)
        self._g_autotick = True                     # False: tests call guide_tick() themselves
        self._gtick_token = None
        self.eng.listeners.append(self._g_enqueue)

    # ---- walk construction -----------------------------------------------------------------------------------------------
    def _g_sections(self, board: str) -> list[tuple[dict, dict]]:
        """(step, section) pairs that have something to walk and apply to the board; the current step first."""
        st = self.eng.state
        steps = self.eng.proto.data["steps"]
        cur = self._step()
        ordered = ([cur] if cur else []) + [s for s in steps if not cur or s["id"] != cur["id"]]
        return [(s, sec) for s in ordered for sec in s.get("sections", [])
                if (sec.get("actions") or sec.get("tests")) and st.sec_applies(s, sec, board)]

    def _g_pick_section(self, words: list[str], section_id: str | None, board: str):
        """(step, section, None) or (None, None, spoken refusal)."""
        cands = self._g_sections(board)
        if section_id:
            hit = [(s, sec) for s, sec in cands if sec["id"] == section_id]
            return (*hit[0], None) if hit else (None, None, "That section does not apply to this board.")
        if words:
            want = set(words)
            hit = [(s, sec) for s, sec in cands if want <= words_of(sec.get("title", ""), sec["id"].replace("_", " "))]
            if len(hit) == 1:
                return (*hit[0], None)
            names = " or ".join(f"guide {spoken_name(sec.get('title', sec['id']))}" for _, sec in (hit or cands)[:4])
            return None, None, (f"More than one section matches {' '.join(words)}. " if hit else f"No section called {' '.join(words)}. ") + f"Say {names}."
        tid, cur = self.ctx["test_id"], self._step()
        if cur:
            mine = [(s, sec) for s, sec in cands if s["id"] == cur["id"]]
            for s, sec in mine:
                if tid and any(t["id"] == tid for t in sec.get("tests", [])):
                    return s, sec, None
            if mine:
                return (*mine[0], None)
        if cands:
            return (*cands[0], None)
        return None, None, "No section to guide here."

    def _g_make(self, step: dict, sec: dict, board: str) -> dict:
        walk = derive_walk(sec)
        return {"step_id": step["id"], "section": sec["id"], "title": sec.get("title", sec["id"]), "sec": sec, "board": board, "walk": walk,
                "i": 0, "states": ["pending"] * len(walk), "run": None, "spoken": set(), "buf": [], "last": "", "hold": None,
                "done": False, "summary": "", "started": time.time()}

    # ---- sentences -----------------------------------------------------------------------------------------------------------
    def _g_test(self, tid: str) -> dict:
        return self.eng.state.test_index[tid][2]

    def _g_detail(self, g: dict, s: dict) -> str:
        if s["kind"] == "manual":
            t = self._g_test(s["id"])
            from .controller import _probe_text
            return _probe_text(t) or first_sentence(t.get("instructions") or "")
        a = s["action"]
        parts = []
        e = energise_phrase(a)
        if e:
            parts.append(e)
        pr = first_sentence(str((a.get("params") or {}).get("prompt") or ""))
        if pr:
            parts.append(pr)
        own = first_sentence(a.get("say") or a.get("instructions") or "")
        if own:
            parts.append(own)
        elif not parts and s["n"] == 1:
            parts.append(first_sentence(g["sec"].get("instructions") or ""))
        if not parts:
            from .controller import _probe_text
            probes = [_probe_text(self._g_test(t)) for t in s["tests"][:1]]
            parts += [p for p in probes if p]
        return " ".join(p for p in parts if p)

    def _g_announce(self, g: dict, intro: bool = False) -> str:
        from .controller import spoken_board
        walk, i = g["walk"], g["i"]
        s = walk[i]
        head = ""
        if intro:
            n = len(walk)
            head = f"{spoken_name(g['title'])}, board {spoken_board(g['board'])}, {number_word(n)} step{'s' if n != 1 else ''}. "
        detail = self._g_detail(g, s)
        if s["kind"] == "manual":
            cue = "Say the value."
            body = f"Step {i + 1} of {len(walk)}: Measure {short_name(s['title'], 8)}. {detail + ' ' if detail else ''}{cue}"
        else:
            body = f"Step {i + 1} of {len(walk)}: {spoken_name(s['title'])}. {detail + ' ' if detail else ''}Say ready."
        return head + body

    def _g_focus(self, g: dict):
        """Point the page at the current step: its first test and the walk's board."""
        s = g["walk"][g["i"]]
        self.ctx["step"] = g["step_id"]
        self.ctx["board"] = g["board"]
        if s["tests"]:
            self.ctx["test_id"] = s["tests"][0]

    def _g_counts(self, g: dict) -> tuple[int, int, int]:
        st, p, r, u = self.eng.state, 0, 0, 0
        for t in g["sec"].get("tests", []):
            c = st.cell(g["board"], t["id"])
            v = c.get("verdict") if c["status"] == "accepted" else None
            if v is None and c["proposals"]:
                v = list(c["proposals"].values())[-1].get("verdict")
            if c["status"] == "skipped" or (c["status"] != "accepted" and not c["proposals"]):
                u += 1
            elif v in ("fail", "review", "invalid"):
                r += 1
            else:
                p += 1
        return p, r, u

    def _g_summary(self, g: dict) -> str:
        p, r, u = self._g_counts(g)
        s = f"Section done. {p} pass" + (f", {r} to review" if r else "") + (f", {u} not read" if u else "") + "."
        return s

    def _g_done_step(self, g: dict, i: int) -> bool:
        s = g["walk"][i]
        if g["states"][i] == "done":
            return True
        if s["kind"] == "manual":
            c = self.eng.state.cell(g["board"], s["id"])
            return c["status"] == "accepted" or bool(c["proposals"])
        return False

    # ---- commands (called by the bench router under the controller lock) -------------------------------------------------
    def _guide_route(self, toks: list[str], base: dict) -> dict | None:
        r = parse_guide(toks, self._g is not None)
        if r is None:
            return None
        return {**base, "intent": "guide", "gcmd": r["cmd"], "gwords": r["words"], "gn": r["n"]}

    def _do_guide(self, res, n, text):
        fn = getattr(self, "_gc_" + res["gcmd"])
        out = fn(res)
        g = self._g
        if g:
            out["guide"] = {"section": g["section"], "n": g["i"] + 1, "total": len(g["walk"])}
        return out

    def _g_no_walk(self) -> dict:
        return {"outcome": "rejected", "detail": "no guide open", "reply": "No guide is open. Say guide me."}

    def guide_start(self, section_id: str | None = None) -> dict:
        """The Guide button of a section: the same as saying "guide <section>". Returns the journal entry of the announcement."""
        text = f"guide {section_id}" if section_id else "guide me"
        with self._serial:
            with self._lock:
                self._utt += 1
                res = {"intent": "guide", "route": "grammar", "transcript": text}
                out = self._gc_start({"gwords": [], "section_id": section_id})
                self._bench_after(res, out)
                entry = self._record(self._utt, "typed", text, res, out, None, None)
            self.tts.say(out["reply"])
            self._publish(entry, nav=out.get("nav", False))
            return entry

    def _gc_start(self, res) -> dict:
        from .controller import spoken_board
        b = {"cmd": "guide_start"}
        cur = self._running()
        if cur:
            return {"outcome": "rejected", "detail": "busy: " + cur["title"], "bench": {**b, "refused": "busy"},
                    "reply": f"{spoken_name(cur['title'])} is still running. Say stop to abort."}
        board, step = self.ctx["board"], self._step()
        if not board:
            return {"outcome": "rejected", "detail": "no board", "reply": "Which board? Say board S1.", "bench": {**b, "refused": "no board"}}
        if step is None and not res.get("section_id"):
            return {"outcome": "rejected", "detail": "no step", "reply": "No step is open. Open a step first.", "bench": {**b, "refused": "no step"}}
        try:
            self.eng._board(board)
            if step is not None:
                self.eng._check_open(board, step)
        except CmdError as e:
            return self._refused(e, b)
        s, sec, why = self._g_pick_section(list(res.get("gwords") or []), res.get("section_id"), board)
        if sec is None:
            return {"outcome": "rejected", "detail": why, "reply": why, "bench": {**b, "refused": "which section"}}
        try:
            self.eng._check_open(board, s)
        except CmdError as e:
            return self._refused(e, b)
        g = self._g_make(s, sec, board)
        if not g["walk"]:
            return {"outcome": "rejected", "detail": "empty walk", "reply": f"{spoken_name(g['title'])} has nothing to run or measure.", "bench": {**b, "refused": "empty"}}
        if self._g and self._g["section"] == g["section"] and self._g["board"] == board and not self._g["done"]:
            self._g_focus(self._g)
            return {"outcome": "applied", "detail": "guide already open", "reply": self._g_announce(self._g, intro=True), "nav": True, "boards": [board]}
        self._g = g
        self._g_ensure_worker()
        self._g_focus(g)
        self._changed()
        return {"outcome": "applied", "detail": f"guide {sec['id']} on {board}, {len(g['walk'])} steps", "boards": [board], "nav": True,
                "reply": self._g_announce(g, intro=True), "bench": {**b, "section": sec["id"], "board": board}}

    def _gc_ready(self, res) -> dict:
        g = self._g
        if g is None:
            return self._g_no_walk()
        s, i = g["walk"][g["i"]], g["i"]
        b = {"cmd": "guide_ready", "board": g["board"], "step": g["step_id"], "n": i + 1}
        if g["done"]:
            return {"outcome": "rejected", "detail": "section done", "reply": "The section is done. Say next section, or end guide."}
        if s["kind"] == "manual":
            return {"outcome": "applied", "detail": "manual step", "reply": f"Say the value for {short_name(s['title'], 8)}."}
        cur = self._running()
        if cur:
            return {"outcome": "rejected", "detail": "busy: " + cur["title"], "bench": {**b, "refused": "busy"},
                    "reply": f"{spoken_name(cur['title'])} is still running. Say stop to abort."}
        if g["states"][i] == "done":
            return {"outcome": "rejected", "detail": "already ran", "reply": "That step is done. Say next step, or redo."}
        return self._g_launch(g, b)

    def _g_launch(self, g: dict, b: dict) -> dict:
        s, i = g["walk"][g["i"]], g["i"]
        a = s["action"]
        act = {"step": g["step_id"], "section": g["section"], "id": a["id"], "title": a["title"], "plugin": a["plugin"], "params": a.get("params") or {}}
        try:
            self.eng._board(g["board"])
            step = self.eng.proto.step(g["step_id"])
            self.eng._check_open(g["board"], step)
        except CmdError as e:
            return self._refused(e, b)
        out = self._guard(lambda: self._bench_launch(act, g["board"], True, b), b)       # ready is the confirmation; the runner re-checks every gate
        if out["outcome"] == "applied":
            g["states"][i] = "running"
            g["run"] = int(out["bench"]["run"])
            g["spoken"], g["buf"] = set(), []
            if g["hold"] and g["hold"]["step"] == i:
                g["hold"] = None
            out["reply"] = f"Step {i + 1}: running {spoken_name(a['title'])}."
            self._changed()
        return out

    def _gc_next(self, res) -> dict:
        g = self._g
        if g is None:
            return self._g_no_walk()
        i = g["i"]
        if self._running():
            return {"outcome": "rejected", "detail": "busy", "reply": "The step is still running. Say stop to abort."}
        if not self._g_done_step(g, i) and g["states"][i] != "skipped":
            return {"outcome": "rejected", "detail": "step not done",
                    "reply": f"Step {i + 1} is not done. Say ready, or skip step."}
        held, errs = self._g_accept_clean(g, i)
        if held:
            return {"outcome": "rejected", "detail": "held fail", "reply": held}
        out = self._g_advance(g, 1)
        if errs:
            out["reply"] = " ".join(errs + [out["reply"]])
        return out

    def _g_accept_clean(self, g: dict, i: int) -> tuple[str, list[str]]:
        """Take the clean pending readings of step i (voice values and instrument proposals) as "accept" does. A pending fail from the
        operator's own value blocks, as for "next test"; held instrument readings stay pending for the operator and count as to review."""
        from .controller import _is_clean, spoken_board
        st = self.eng.state
        self._drop_pending()
        errs: list[str] = []
        for tid in g["walk"][i]["tests"]:
            c = st.cell(g["board"], tid)
            for q in list(c["proposals"].values()):
                src = q.get("source") or {}
                item = {"verdict": q.get("verdict"), "flags": q.get("flags") or []}
                if src.get("kind") == "voice" and not _is_clean(item):
                    return "Pending fail: say accept fail or reject first.", errs
                if not _is_clean(item):
                    continue
                try:
                    self.eng.accept(g["board"], tid, q["id"], confirm=False)
                except CmdError as e:
                    errs.append(f"Could not accept {tid}: {e}.")
        return "", errs

    def _g_advance(self, g: dict, d: int) -> dict:
        i = g["i"] + d
        if i >= len(g["walk"]):
            g["done"] = True
            g["hold"] = None
            g["summary"] = self._g_summary(g)
            self._changed()
            return {"outcome": "applied", "detail": "section done", "reply": g["summary"] + " Say next section, or end guide.", "nav": True}
        if i < 0:
            return {"outcome": "rejected", "detail": "first step", "reply": "This is the first step."}
        g["i"] = i
        g["done"] = False
        self._g_focus(g)
        self._changed()
        return {"outcome": "applied", "detail": f"step {i + 1}", "reply": self._g_announce(g), "nav": True}

    def _gc_prev(self, res) -> dict:
        g = self._g
        if g is None:
            return self._g_no_walk()
        if self._running():
            return {"outcome": "rejected", "detail": "busy", "reply": "The step is still running. Say stop to abort."}
        if g["done"]:
            g["done"] = False
            return self._g_advance(g, 0)
        return self._g_advance(g, -1)

    def _gc_repeat(self, res) -> dict:
        g = self._g
        if g is None:
            return self._g_no_walk()
        if g["done"]:
            return {"outcome": "applied", "detail": "repeat", "reply": g["summary"]}
        self._g_focus(g)
        return {"outcome": "applied", "detail": "repeat", "reply": self._g_announce(g), "nav": True}

    def _gc_where(self, res) -> dict:
        return self._gc_repeat(res)

    def _gc_redo(self, res) -> dict:
        g = self._g
        if g is None:
            return self._g_no_walk()
        if res.get("gn"):
            if not 1 <= res["gn"] <= len(g["walk"]):
                return {"outcome": "rejected", "detail": "no such step", "reply": f"There are {number_word(len(g['walk']))} steps."}
            g["i"], g["done"] = res["gn"] - 1, False
            self._g_focus(g)
        s, i = g["walk"][g["i"]], g["i"]
        if s["kind"] == "manual":
            return {"outcome": "applied", "detail": "manual redo", "nav": True,
                    "reply": f"Say the value for {short_name(s['title'], 8)}. Reject the old one first if it is wrong."}
        if self._running():
            return {"outcome": "rejected", "detail": "busy", "reply": "The step is still running. Say stop to abort."}
        g["states"][i] = "pending"
        return self._g_launch(g, {"cmd": "guide_redo", "board": g["board"], "step": g["step_id"], "n": i + 1})

    def _gc_skip(self, res) -> dict:
        g = self._g
        if g is None:
            return self._g_no_walk()
        if self._running():
            return {"outcome": "rejected", "detail": "busy", "reply": "The step is still running. Say stop to abort."}
        i = g["i"]
        if g["states"][i] != "done":
            g["states"][i] = "skipped"
        return self._g_advance(g, 1)

    def _gc_goto(self, res) -> dict:
        g = self._g
        if g is None:
            return self._g_no_walk()
        n = res.get("gn")
        if not n or not 1 <= n <= len(g["walk"]):
            return {"outcome": "rejected", "detail": "no such step", "reply": f"There are {number_word(len(g['walk']))} steps."}
        if self._running():
            return {"outcome": "rejected", "detail": "busy", "reply": "The step is still running. Say stop to abort."}
        g["i"], g["done"] = n - 1, False
        self._g_focus(g)
        self._changed()
        return {"outcome": "applied", "detail": f"step {n}", "reply": self._g_announce(g), "nav": True}

    def _gc_end(self, res) -> dict:
        g = self._g
        if g is None:
            return self._g_no_walk()
        run = self._running()
        self._g = None
        self._gtick_token = None
        self._changed()
        tail = " The action is still running: say stop to abort it." if run else ""
        return {"outcome": "applied", "detail": "guide ended", "reply": "Guide ended." + tail}

    def _gc_next_section(self, res) -> dict:
        g = self._g
        board = (g or {}).get("board") or self.ctx["board"]
        if not board:
            return {"outcome": "rejected", "detail": "no board", "reply": "Which board? Say board S1."}
        if g is not None and not g["done"]:
            return {"outcome": "rejected", "detail": "section not done", "reply": f"{spoken_name(g['title'])} is not done. Say next step."}
        cands = self._g_sections(board)
        sid = g["step_id"] if g is not None else self.ctx["step"]
        seq = [(s["id"], sec["id"]) for s, sec in cands if s["id"] == sid]            # the sections of this step, protocol order
        nxt = None
        if g is not None:
            key = (g["step_id"], g["section"])
            k = seq.index(key) if key in seq else -1
            nxt = seq[k + 1] if k + 1 < len(seq) else None
        elif seq:
            nxt = seq[0]
        if nxt is None:
            return {"outcome": "rejected", "detail": "no more sections", "reply": "No more sections here. Say end guide."}
        self._g = None
        self.ctx["board"] = board
        self.ctx["step"] = nxt[0]
        return self._gc_start({"gwords": [], "section_id": nxt[1]})

    # ---- hooks into the rest of the controller -------------------------------------------------------------------------------
    def _guide_after(self, res: dict, out: dict):
        """After an interpretation was applied: a spoken value on a manual step is read back in the guide's words, and the cursor stays
        on the walk's board (row mode would move it on)."""
        g = self._g
        if g is None:
            return
        intent = res.get("intent")
        if intent in ("set_board", "bench_board") and out.get("outcome") == "applied" and self.ctx["board"] != g["board"]:
            g["board"] = self.ctx["board"]
            g["i"], g["done"], g["states"], g["hold"] = 0, False, ["pending"] * len(g["walk"]), None
            self._g_focus(g)
            out["reply"] = f"{out['reply']} {self._g_announce(g, intro=True)}"
            out["nav"] = True
            self._changed()
            return
        if intent != "values" or out.get("outcome") != "proposed" or not self.last_props:
            return
        s = g["walk"][g["i"]]
        item = next((m for m in self.last_props if m["board"] == g["board"] and m["test_id"] in s["tests"]), None)
        if s["kind"] != "manual" or item is None:
            return
        from .controller import _is_clean, unit_words
        self.ctx["board"], self.ctx["test_id"] = g["board"], item["test_id"]
        phrase = reading_phrase(item["test"], {"value": item["value"], "unit": item["unit"], "verdict": item["verdict"], "flags": item["flags"]}, unit_words)
        g["states"][g["i"]] = "done"
        g["last"] = phrase
        out["reply"] = phrase + (" Say next step." if _is_clean(item) else " Say accept fail, or reject.")
        out["nav"] = True
        self._changed()

    def _guide_stopped(self):
        """STOP acted (benchcmd.bench_stop_now): the running step is paused, not failed."""
        g = self._g
        if g is not None and g["states"][g["i"]] == "running":
            g["states"][g["i"]] = "paused"
            g["buf"] = []
            self._changed()

    def guide_tail(self) -> str:
        g = self._g
        if g is None or g["states"][g["i"]] != "paused":
            return ""
        return f" Guide paused on step {g['i'] + 1}. Say ready to run it again."

    # ---- engine events -> speech ---------------------------------------------------------------------------------------------
    def _g_ensure_worker(self):
        if self._gworker is None or not self._gworker.is_alive():
            self._gworker = threading.Thread(target=self._g_loop, daemon=True, name="guide-events")
            self._gworker.start()

    def _g_enqueue(self, ev: dict):
        """Engine listener: called under the engine's emit lock, so it only queues (the worker takes the controller lock)."""
        if self._g is not None and ev.get("type") in ("proposal", "accept", "action_start", "action_end"):
            self._gq.put(ev)

    def _g_loop(self):
        while True:
            ev = self._gq.get()
            if ev is None:
                return
            try:
                self._g_event(ev)
            except Exception as e:                       # a speech helper must never kill the worker
                self.tts.log.append(f"[guide error: {e}]")

    def _g_is_reading(self, ev: dict) -> bool:
        p = ev["payload"]
        src = p.get("source") or {}
        if src.get("kind") != "action" or p.get("via") == "backfill":
            return False
        return ev["type"] == "proposal" or (ev["type"] == "accept" and bool(p.get("auto")))

    def _g_event(self, ev: dict):
        say: list[str] = []
        with self._lock:
            g = self._g
            if g is None:
                return
            t, p = ev["type"], ev["payload"]
            i, s = g["i"], g["walk"][g["i"]]
            if t == "action_start":
                if (g["run"] is None and s["kind"] == "action" and p.get("action") == s["id"] and p.get("board") == g["board"]
                        and g["states"][i] in ("pending", "paused", "failed", "lapsed")):
                    g["states"][i], g["run"], g["spoken"], g["buf"] = "running", p["run"], set(), []      # started from the page: adopt it
                    self._changed()
            elif self._g_is_reading(ev):
                tid, board = p.get("test_id"), p.get("board")
                if board != g["board"] or tid not in s["tests"] or (board, tid) in g["spoken"] or g["states"][i] not in ("running", "done"):
                    return
                g["spoken"].add((board, tid))
                from .controller import unit_words
                phrase = reading_phrase(self._g_test(tid), p, unit_words)
                g["last"] = phrase
                if g["states"][i] == "running":
                    g["buf"].append(phrase)
                else:
                    say.append(phrase)
                self._changed()
            elif t == "action_end" and p.get("run") == g["run"]:
                say = self._g_finished(g, i, p)
        if say:
            self._g_say(" ".join(say), "guide")

    def _g_finished(self, g: dict, i: int, p: dict) -> list[str]:
        g["run"] = None
        st, s = p.get("state"), g["walk"][i]
        buf, g["buf"] = g["buf"], []
        if st == "cancelled":
            g["states"][i] = "paused"
            self._changed()
            return []
        if st != "ok":
            g["states"][i] = "failed"
            self._changed()
            return [reason_phrase(p.get("text", "")) + ".", "Say redo to try again, or skip step."]
        g["states"][i] = "done"
        out = list(buf)
        hold = hold_s(s["action"])
        leave = (s["action"].get("params") or {}).get("leave") == "on" or hold is not None
        if leave and hold:
            g["hold"] = {"step": i, "start": self._gnow(), "window": hold, "warned": False}
            out.append(f"Hold window {sig3(hold)} seconds.")
            self._g_start_ticker(g)
        elif leave:
            out.append("The output stays on until the watchdog switches it off.")
        if not buf and not s["tests"]:
            out.insert(0, "Done.")
        out.append("Say next step." if i + 1 < len(g["walk"]) else "Last step. Say next step to finish.")
        self._changed()
        return out

    def _g_say(self, text: str, why: str):
        with self._lock:
            self._utt += 1
            g = self._g
            out = {"outcome": "applied", "detail": why, "reply": text, "boards": [g["board"]] if g else []}
            if g:
                out["guide"] = {"section": g["section"], "n": g["i"] + 1, "total": len(g["walk"])}
            entry = self._record(self._utt, "system", "", {"intent": "guide_say", "route": "none"}, out, None, None)
        self.tts.say(text)
        self._publish(entry)

    # ---- the hold window ----------------------------------------------------------------------------------------------------
    def _g_start_ticker(self, g: dict):
        token = object()
        self._gtick_token = token
        if not self._g_autotick:
            return

        def run():
            while self._gtick_token is token and self._g is not None and self._g.get("hold"):
                time.sleep(0.25)
                self.guide_tick()
        threading.Thread(target=run, daemon=True, name="guide-hold").start()

    def guide_left(self) -> float | None:
        g = self._g
        h = g and g.get("hold")
        return None if not h else max(0.0, h["window"] - (self._gnow() - h["start"]))

    def guide_tick(self):
        """Hold-window clock: the 15 second warning and the lapse. Called by the ticker thread, or by a test with a fake clock."""
        text = None
        with self._lock:
            g = self._g
            h = g and g.get("hold")
            if not h:
                return
            left = h["window"] - (self._gnow() - h["start"])
            if left <= 0:
                g["hold"] = None
                if g["states"][h["step"]] == "done":
                    g["states"][h["step"]] = "lapsed"
                text = f"The hold lapsed and the output is off. Say redo step {h['step'] + 1}."
                self._changed()
            elif left <= HOLD_WARN_S and not h["warned"] and h["window"] > HOLD_WARN_S:
                h["warned"] = True
                text = f"{int(HOLD_WARN_S)} seconds left on the hold."
                self._changed()
        if text:
            self._g_say(text, "hold")

    # ---- status for the page ------------------------------------------------------------------------------------------------
    def guide_status(self) -> dict | None:
        g = self._g
        if g is None:
            return None
        s = g["walk"][g["i"]]
        left = self.guide_left()
        return {"section": g["section"], "title": g["title"], "board": g["board"], "n": g["i"] + 1, "total": len(g["walk"]),
                "step": {"title": s["title"], "kind": s["kind"], "state": g["states"][g["i"]], "text": self._g_detail(g, s)},
                "steps": [{"n": x["n"], "title": x["title"], "state": g["states"][k]} for k, x in enumerate(g["walk"])],
                "last": g["last"], "done": g["done"], "summary": g["summary"],
                "hold": None if left is None else {"left_s": round(left, 1), "window_s": g["hold"]["window"]}}
