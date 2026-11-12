"""Hands-free bench commands: board select, start / stop / confirm of the current action (docs/VOICE.md, "Bench commands").

Two halves. The functions at the top are pure: tokens in, a small dict out; no engine, no runner, no I/O. `BenchCommands` is a mixin
for VoiceController: it turns a parse into the same calls the UI makes (the runner's start and cancel, the engine's gates) and keeps
the one pending start request.

Safety rules kept here:
- STOP is matched by this local grammar before the language model and before the controller's utterance lock, and never asks for
  confirmation. While an action runs, any short utterance containing stop, abort or halt aborts it (a false stop is harmless).
- START of an action that can energise or write to hardware is read back and waits for "confirm". Nothing in the plugin API says
  which actions energise, so every non-simulated action is treated as one (see `needs_confirm`). In simulate mode the confirmation
  can be switched off with the voice setting `confirm_simulated`; an action with an operator prompt always needs it.
- Every gate stays where it was: the engine's step gate, power interlock and stops (`Engine._check_open`), and the runner's one
  action at a time. This module adds no way around them.
"""
from __future__ import annotations

import asyncio
import re
import threading
import time

from ..engine import CmdError
from ..protocol import seq_items
from . import grammar
from .tts import strip_brackets

STOP_WORDS = {"stop", "abort", "halt"}
STOP_QUALIFIERS = {"test", "action", "run", "it", "that", "this", "everything", "now", "all", "please", "the", "okay", "uh", "um", "right"}
START_VERBS = {"start", "run", "begin", "execute", "launch"}
START_NOISE = {"test", "tests", "action", "it", "this", "that", "the", "a", "now", "please", "okay", "uh", "um", "current", "next", "just", "again"}
BOARD_NOISE = {"please", "the", "go", "to", "select", "switch", "set", "change", "use", "number", "now", "goto", "pick", "on", "uh", "um", "okay"}
CONFIRM_FORMS = ([("confirm",), ("confirmed",), ("yes", "confirm"), ("yes", "confirmed"), ("yeah", "confirm"), ("yep", "confirm")])
CONFIRM_FILL = {"please", "okay", "uh", "um", "the", "it"}
DEDUPE_S = 3.0           # a final "stop" right after a stop that already acted (a partial, or "stop stop") is not answered again
EXPIRED_S = 15.0         # a "confirm" this soon after a timed-out request is refused, never taken as "accept"


def tokens(text: str) -> list[str]:
    toks, _ = grammar._tokens((text or "").replace("'", ""))
    return [t.lower() for t in toks]


# --------------------------------------------------------------------------------------------- pure grammar

def stop_kind(toks: list[str], running: bool) -> str | None:
    """'abort' (an action is running and the utterance is a stop), 'idle' (a test-specific stop with nothing running),
    'bare' (a plain "stop" with nothing running: the older cancel-the-proposal meaning stays), or None."""
    has_stop = any(t in STOP_WORDS for t in toks)
    cancel_test = bool(toks) and toks[0] == "cancel" and len(toks) <= 3 and any(t in ("test", "action", "run") for t in toks[1:])
    if running:
        if (has_stop and len(toks) <= 6) or cancel_test:
            return "abort"
        return None
    if cancel_test:
        return "idle"
    if has_stop and all(t in STOP_WORDS or t in STOP_QUALIFIERS for t in toks):
        return "bare" if all(t == "stop" for t in toks) else "idle"
    return None


def is_confirm(toks: list[str]) -> bool:
    core = tuple(t for t in toks if t not in CONFIRM_FILL)
    return core in CONFIRM_FORMS


def parse_start(toks: list[str]) -> list[str] | None:
    """None when this is not a start command, else the words naming the action ([] = the current action)."""
    core = [t for t in toks if t not in ("please", "uh", "um", "okay", "hey")]
    if not core or core[0] not in START_VERBS:
        return None
    return [t for t in core[1:] if t not in START_NOISE]


def parse_board(toks: list[str]):
    """('board', n) for "board S1", "board one", "go to board 3", "board s one"; None when it is not a board command."""
    core = [t for t in toks if t not in BOARD_NOISE]
    if not core or core[0] not in ("board", "boards"):
        return None
    if len(core) > 2 and core[1] in ("s", "es", "ess", "b", "bee"):       # "board s one", "board S 2": the batch prefix spoken as a letter
        core = core[:1] + core[2:]
    r = grammar._board_at(core, 0)
    if r is None or r[0] == "all" or len(r[0]) != 1 or r[1] != len(core):
        return None
    return ("board", r[0][0])


def words_of(*parts: str) -> set[str]:
    return {w for p in parts for w in re.findall(r"[a-z0-9]+", (p or "").lower())}


def spoken_name(title: str) -> str:
    return re.sub(r"\s+", " ", strip_brackets(title or "")).strip()


# --------------------------------------------------------------------------------------------- the mixin

class BenchCommands:
    """Mixed into VoiceController (self.eng, self.sess, self.ctx, self.cfg, self.tts, self._lock, self._bc, self._record are its)."""

    def _bench_init(self):
        self._bp: dict | None = None            # the pending start request {action..., deadline, timer, utt}
        self._bexpired = 0.0                     # monotonic time of the last timed-out request
        self._bstop_at = 0.0                     # monotonic time of the last stop that acted
        self._bnote = ""                         # "Start cancelled." for the reply of the utterance that cancelled a request
        self.bench_last: dict | None = None      # last bench command for the UI line

    # ---- runner access --------------------------------------------------------------------------------------
    def _running(self) -> dict | None:
        r = getattr(self.sess, "runner", None)
        return r.status() if r is not None else None

    def _await(self, coro, timeout: float = 10.0):
        loop = getattr(self.sess, "loop", None)
        if loop is None or not loop.is_running():
            coro.close()
            raise CmdError("the server is not ready to run actions yet, open the page and try again", 503)
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    def _labels(self, msg: str) -> str:
        """Board labels in a message as they are spoken (S01 -> S1)."""
        from .controller import spoken_board
        for b in sorted((x["label"] for x in self.eng.state.boards), key=len, reverse=True):
            msg = re.sub(rf"\b{re.escape(b)}\b", spoken_board(b), msg)
        return msg

    @staticmethod
    def _bench_base(text: str) -> dict:
        return {"transcript": text, "route": "grammar", "model": None, "values": [], "boards": [], "test_id": None, "text": "",
                "reason": None, "grammar": {"ok": True, "reason": None, "fields": None}, "llm": None}

    # ---- routing ----------------------------------------------------------------------------------------------
    def bench_is_stop(self, text: str) -> bool:
        """True when `text` is a stop that must act now: an action is running and the words say stop. No lock, no model."""
        return self._running() is not None and stop_kind(tokens(text), True) == "abort"

    def _bench_route(self, text: str, base: dict) -> dict | None:
        """The interpretation of a bench command (under the controller lock), or None for any other utterance. Also drops a pending
        start request when the utterance is anything but "confirm"."""
        toks = tokens(text)
        pend = self._bp
        conf = is_confirm(toks)
        sq = self._seq_route(toks, base, pend, conf)
        if sq:
            return sq
        if pend and not conf:
            self._bench_drop("cancelled")
            self._bnote = "Start cancelled."
        if conf:
            if pend:
                return {**base, "intent": "bench_confirm"}
            if time.monotonic() - self._bexpired < EXPIRED_S:
                return {**base, "intent": "bench_expired"}
            return None                                          # no request: "confirm" keeps its older meaning, accept
        sk = stop_kind(toks, self._running() is not None)
        if sk == "abort":
            return {**base, "intent": "bench_stop"}
        if sk == "idle" or (sk == "bare" and pend):
            self._bnote = ""                                       # the reply says it already
            return {**base, "intent": "bench_stop_idle", "reason": "pending" if pend else ""}
        if sk == "bare" and time.monotonic() - self._bstop_at < DEDUPE_S:
            return {**base, "intent": "bench_stop_idle", "reason": "dup"}
        g = self._guide_route(toks, base)                           # "guide me", and while a walk is open ready / next step / repeat ...
        if g:
            return g
        b = parse_board(toks)
        if b:
            return {**base, "intent": "bench_board", "board_n": b[1]}
        words = parse_start(toks)
        if words is not None:
            return {**base, "intent": "bench_start", "words": words}
        return None

    # ---- stop (fast path) -----------------------------------------------------------------------------------------
    def bench_stop_now(self, text: str, source: str, audio=None, stt_ms=None, t0: float | None = None) -> dict:
        """Abort the running action. Called before the utterance lock and the model; the cancel is sent first, the journal and the
        speech follow. The time from `t0` (the text arrived) to the cancel written is journalled as cancel_ms."""
        t0 = time.perf_counter() if t0 is None else t0
        run = self._running()
        try:
            self._await(self.sess.runner.cancel(), 5.0)
            outcome, reply, detail = "applied", "Stopping.", "abort sent"
        except CmdError as e:
            outcome, reply, detail = "rejected", ("Nothing is running." if "nothing is running" in str(e) else f"Stop failed: {e}"), str(e)
        cancel_ms = round(1000 * (time.perf_counter() - t0), 1)
        self._bstop_at = time.monotonic()
        self.tts.stop()
        with self._lock:
            self._bench_drop("stopped")
            if outcome == "applied":
                self._guide_stopped()
            self._utt += 1
            res = {"transcript": text, "route": "grammar", "intent": "bench_stop", "model": None}
            out = {"outcome": outcome, "detail": f"{detail}; cancel sent {cancel_ms} ms after the text arrived", "reply": reply,
                   "bench": {"cmd": "stop", "action": (run or {}).get("action"), "board": (run or {}).get("board"),
                             "run": (run or {}).get("run"), "cancel_ms": cancel_ms}}
            entry = self._record(self._utt, source, text, res, out, audio, stt_ms)
            self.bench_last = {"text": text, "reply": reply, "outcome": outcome, "ts": time.time()}
        self.tts.say(reply)
        self._publish(entry)
        if outcome == "applied":
            threading.Thread(target=self._bench_watch_stopped, args=(run,), daemon=True, name="bench-stop-watch").start()
        return entry

    def handle_partial(self, text: str) -> bool:
        """For a streaming speech-to-text: a partial transcript that already says stop aborts the running action at once.
        Returns True when it acted. The speech-to-text in use does not stream today, so nothing calls this yet."""
        if self.bench_is_stop(text):
            self.bench_stop_now(text, "partial")
            return True
        return False

    def _bench_watch_stopped(self, run: dict | None, limit_s: float = 120.0):
        """Say "Stopped." once the run has ended and the safe state is done (the plugin owns its timeouts; this only waits to tell)."""
        end = time.monotonic() + limit_s
        while time.monotonic() < end:
            cur = self._running()
            if cur is None or (run and cur.get("run") != run.get("run")):
                break
            time.sleep(0.05)
        else:
            return
        with self._lock:
            self._utt += 1
            reply = "Stopped." + self.guide_tail()
            out = {"outcome": "applied", "detail": "run ended, safe state done", "reply": reply,
                   "bench": {"cmd": "stopped", "run": (run or {}).get("run")}}
            entry = self._record(self._utt, "system", "", {"intent": "bench_stopped", "route": "none"}, out, None, None)
        self.tts.say(reply)
        self._publish(entry)

    # ---- actions of the current step -----------------------------------------------------------------------------------
    def _bench_actions(self, step: dict, board: str) -> list[dict]:
        out = []
        for sec in step.get("sections", []):
            if not self.eng.state.sec_applies(step, sec, board):
                continue
            for a in sec.get("actions", []):
                out.append({"step": step["id"], "section": sec["id"], "id": a["id"], "title": a["title"], "sec_title": sec.get("title", ""),
                            "plugin": a["plugin"], "params": a.get("params") or {},
                            "tests": {t["id"] for t in sec.get("tests", [])}})
        return out

    def _bench_done(self, board: str, step_id: str, action_id: str) -> bool:
        return any(a.get("board") == board and a.get("step") == step_id and a.get("action") == action_id and a.get("state") == "ok"
                   for a in self.eng.state.actions)

    def _bench_pick(self, step: dict, board: str, words: list[str]):
        """(action, None) or (None, spoken refusal)."""
        acts = self._bench_actions(step, board)
        if not acts:
            return None, f"{step['title']} has no action to run."
        if words:
            want = set(words)
            hits = [a for a in acts if want <= words_of(a["title"], a["sec_title"], a["id"].replace("_", " "))]
            if len(hits) == 1:
                return hits[0], None
            opts = hits or acts
            return None, (f"More than one action matches {' '.join(words)}. " if hits else f"No action called {' '.join(words)}. ") + self._options(opts)
        tid = self.ctx["test_id"]
        focus = [a for a in acts if tid in a["tests"]] or acts
        if len(focus) == 1:
            return focus[0], None
        todo = [a for a in focus if not self._bench_done(board, a["step"], a["id"])]
        if len(todo) == 1:
            return todo[0], None
        return None, "More than one action here. " + self._options(todo or focus)

    @staticmethod
    def _options(acts: list[dict]) -> str:
        names = [spoken_name(a["title"]) for a in acts[:4]]
        return "Say " + " or ".join(f"run {n}" for n in names) + "."

    def needs_confirm(self, act: dict) -> bool:
        """Does starting this action need a spoken "confirm"? Nothing in the plugin API marks the actions that energise a rail, flash
        or drive a bus, so every real (non-simulated) action does; in simulate mode only when `confirm_simulated` is on; an action
        with an operator prompt (its physical set-up question) always does, because that is what the UI asks too."""
        if str(act["params"].get("prompt") or "").strip():
            return True
        if not self.sess.simulate:
            return True
        return bool(self.cfg.get("confirm_simulated", True))

    def _readback(self, act: dict, board: str) -> str:
        from .controller import spoken_board
        volts = act["params"].get("volts")
        extra = f", {volts:g} volts" if isinstance(volts, (int, float)) and not isinstance(volts, bool) else ""
        prompt = str(act["params"].get("prompt") or "").strip()
        p = f" {prompt}" if prompt else ""
        return f"Run {spoken_name(act['title'])} on {spoken_board(board)}{extra}.{p} Say confirm."

    # ---- the commands ---------------------------------------------------------------------------------------------------
    def _do_bench_board(self, res, n, text):
        from .controller import spoken_board
        labels = [b["label"] for b in self.eng.state.boards]
        k = res["board_n"]
        if k > len(labels):
            pre = re.match(r"[A-Za-z]+", labels[0]).group(0) if labels else "S"
            return {"outcome": "rejected", "detail": f"no board {k}", "reply": f"No board {pre}{k}.", "bench": {"cmd": "board", "board": None}}
        lab = labels[k - 1]
        self.ctx["board"] = lab
        return {"outcome": "applied", "detail": f"board {lab}", "reply": f"Board {spoken_board(lab)}.", "nav": True, "boards": [lab],
                "bench": {"cmd": "board", "board": lab}}

    def _do_bench_start(self, res, n, text):
        from .controller import spoken_board
        b = {"cmd": "start"}
        cur = self._running()
        if cur:
            return {"outcome": "rejected", "detail": "busy: " + cur["title"], "reply": f"{spoken_name(cur['title'])} is still running. Say stop to abort.",
                    "bench": {**b, "refused": "busy"}}
        board, step = self.ctx["board"], self._step()
        if not board:
            return {"outcome": "rejected", "detail": "no board", "reply": "Which board? Say board S1.", "bench": {**b, "refused": "no board"}}
        if step is None:
            return {"outcome": "rejected", "detail": "no step", "reply": "No step is open. Open a step first.", "bench": {**b, "refused": "no step"}}
        try:
            self.eng._board(board)
            self.eng._check_open(board, step)                # CmdError: step gate, power interlock / hold, stop on the board
        except CmdError as e:
            return self._refused(e, b)
        if step.get("kind") == "automated" and not res.get("words"):
            return self._seq_ask_run(step, board, n, b)
        act, why = self._bench_pick(step, board, list(res.get("words") or []))
        if act is None:
            return {"outcome": "rejected", "detail": why, "reply": why, "bench": {**b, "refused": "which action"}}
        b.update(board=board, step=act["step"], action=act["id"])
        if not self.needs_confirm(act):
            return self._guard(lambda: self._bench_launch(act, board, False, b), b)
        say = self._readback(act, board)
        timeout = float(self.cfg.get("confirm_timeout_s", 10))
        token = object()
        t = threading.Timer(timeout, self._bench_expire, (token,))
        t.daemon = True
        self._bp = {"act": act, "board": board, "text": say, "t0": time.monotonic(), "timeout": timeout, "timer": t, "token": token, "utt": n,
                    "prompt": bool(str(act["params"].get("prompt") or "").strip())}
        t.start()
        self._changed()
        return {"outcome": "question", "detail": f"confirm {act['id']} on {board} within {timeout:g} s", "reply": say, "boards": [board],
                "bench": {**b, "confirm": "asked"}}

    def _refused(self, e: CmdError, b: dict) -> dict:
        return {"outcome": "rejected", "detail": str(e), "reply": self._labels(f"Refused: {e}"), "bench": {**b, "refused": str(e)[:120]}}

    def _guard(self, fn, b: dict) -> dict:
        try:
            return fn()
        except CmdError as e:
            return self._refused(e, b)

    def _bench_launch(self, act: dict, board: str, confirmed: bool, b: dict) -> dict:
        from .controller import spoken_board
        r = self._await(self.sess.runner.start(act["step"], act["section"], act["id"], board, confirmed))
        if r.get("needs_prompt"):                              # only when started without a confirm: ask for it
            return {"outcome": "rejected", "detail": "operator prompt", "reply": f"{r['needs_prompt']} Say start test, then confirm.",
                    "bench": {**b, "refused": "prompt"}}
        if not r.get("ok"):
            return {"outcome": "rejected", "detail": "could not start", "reply": f"Could not start {spoken_name(act['title'])}.",
                    "bench": {**b, "refused": "spawn"}}
        return {"outcome": "applied", "detail": f"run {r['run']}: {act['id']} on {board}", "boards": [board],
                "reply": f"Running {spoken_name(act['title'])} on {spoken_board(board)}.", "bench": {**b, "run": r["run"], "confirm": "confirmed" if confirmed else "skipped"}}

    def _do_bench_confirm(self, res, n, text):
        p = self._bp
        self._bench_drop("confirmed")
        if p.get("seq"):
            return self._seq_confirmed(p)
        b = {"cmd": "confirm", "board": p["board"], "step": p["act"]["step"], "action": p["act"]["id"]}
        cur = self._running()
        if cur:
            return {"outcome": "rejected", "detail": "busy: " + cur["title"], "reply": f"{spoken_name(cur['title'])} is still running. Say stop to abort.",
                    "bench": {**b, "refused": "busy"}}
        return self._guard(lambda: self._bench_launch(p["act"], p["board"], True, b), b)       # the runner re-checks every gate now

    def _do_bench_expired(self, res, n, text):
        return {"outcome": "rejected", "detail": "confirm after timeout", "bench": {"cmd": "confirm", "refused": "expired"},
                "reply": "That request timed out. Nothing started. Say start test again."}

    def _do_bench_stop(self, res, n, text):             # reached only if the fast path was bypassed: do the same thing
        run = self._running()
        try:
            self._await(self.sess.runner.cancel(), 5.0)
        except CmdError as e:
            return {"outcome": "rejected", "detail": str(e), "reply": "Nothing is running."}
        self._bstop_at = time.monotonic()
        self._guide_stopped()
        return {"outcome": "applied", "detail": "abort sent", "reply": "Stopping.", "bench": {"cmd": "stop", "run": (run or {}).get("run")}}

    def _do_bench_stop_idle(self, res, n, text):
        why = res.get("reason")
        if why == "dup":
            return {"outcome": "applied", "detail": "stop already sent", "reply": ""}
        if why == "pending":
            return {"outcome": "applied", "detail": "start request cancelled", "reply": "Cancelled. Nothing started.", "bench": {"cmd": "stop", "refused": "none running"}}
        return {"outcome": "rejected", "detail": "nothing running", "reply": "Nothing is running.", "bench": {"cmd": "stop", "refused": "none running"}}

    # ---- pending request ---------------------------------------------------------------------------------------------------
    def _bench_drop(self, why: str):
        p = self._bp
        if p:
            p["timer"].cancel()
            self._bp = None
            self._changed()

    def _bench_expire(self, token):
        with self._lock:
            p = self._bp
            if not p or p["token"] is not token:
                return
            self._bp = None
            self._bexpired = time.monotonic()
            self._utt += 1
            out = {"outcome": "rejected", "detail": f"no confirm within {p['timeout']:g} s: request cancelled", "boards": [p["board"]],
                   "reply": "No confirmation. Nothing started.",
                   "bench": {"cmd": "start", "board": p["board"], "action": p["act"]["id"], "confirm": "timeout"}}
            entry = self._record(self._utt, "timer", "", {"intent": "bench_timeout", "route": "none"}, out, None, None)
            self.bench_last = {"text": "start (no confirm)", "reply": out["reply"], "outcome": "rejected", "ts": time.time()}
        self.tts.say(out["reply"])
        self._publish(entry)

    def bench_status(self) -> dict:
        """For /api/voice/status: the pending confirmation with its countdown, the last command, the running action."""
        p = self._bp
        conf = None
        if p:
            left = max(0.0, p["timeout"] - (time.monotonic() - p["t0"]))
            conf = {"text": p["text"], "title": spoken_name(p["act"]["title"]), "board": p["board"], "deadline_s": round(left, 2)}
        cur = self._running()
        return {"confirm": conf, "last": self.bench_last, "running": ({"title": cur["title"], "board": cur["board"], "state": cur.get("state")} if cur else None)}

    def _bench_after(self, res: dict, out: dict):
        """Called after an interpretation was applied: the line for the UI, and the note for a request this utterance cancelled."""
        if self._bnote:
            if out.get("reply"):
                out["reply"] = f"{self._bnote} {out['reply']}"
            else:
                out["reply"] = self._bnote
            out["detail"] = f"{out.get('detail', '')}; start request cancelled".strip("; ")
            self._bnote = ""
        if str(res.get("intent", "")).startswith("bench_"):
            self.bench_last = {"text": res.get("transcript", ""), "reply": out.get("reply", ""), "outcome": out.get("outcome"), "ts": time.time()}


# --------------------------------------------------------------------------------------------- automated step (AS-8, AS-11, AS-12)

NO_WORDS = {"no", "nope", "nah", "negative"}
YES_WORDS = {"yes", "yeah", "yep", "yup", "correct"}


class SequenceVoice:
    """Mixed into VoiceController next to BenchCommands: "run test" on an automated step, the operator question, accept and proceed,
    record defects. Everything it does is an operator route: the same SequenceRunner calls the buttons make. A hash is never spoken."""

    def _sq(self):
        return getattr(self.sess, "sequencer", None)

    def _seq_ask_info(self):
        sq = self._sq()
        lv = sq.status() if sq is not None else None
        return (lv or {}).get("ask")

    def _seq_ready(self):
        """(board, step, record) when the open step is automated and its board holds a finished, unrecorded run."""
        step, board = self._step(), self.ctx.get("board")
        if not step or step.get("kind") != "automated" or not board:
            return None
        rec = self.eng.state.sequences.get((board, step["id"]))
        if rec and rec.get("outcome") in ("complete", "stopped", "error", "aborted") and not rec.get("proceed") and not rec.get("defects"):
            return board, step, rec
        return None

    def _seq_image_label(self, step):
        im = self._sq().default_image(step) if self._sq() else None
        return spoken_name((im or {}).get("label", ""))

    def _seq_route(self, toks, base, pend, conf):
        sq = self._sq()
        if sq is None:
            return None
        core = [t for t in toks if t not in ("please", "uh", "um", "okay", "ok", "the", "hey")]
        ask = self._seq_ask_info()
        if ask and (sq.live or {}).get("board") == self.ctx.get("board"):
            if ask.get("kind") == "bench":
                if conf:
                    return {**base, "intent": "seq_bench", "action": "confirm"}
                if core[:2] in (["check", "again"], ["recheck"]) or core == ["recheck"]:
                    return {**base, "intent": "seq_bench", "action": "recheck"}
            elif core:
                if conf or core[0] in YES_WORDS:
                    return {**base, "intent": "seq_answer", "value": "yes", "note": ""}
                if core[0] in NO_WORDS:
                    return {**base, "intent": "seq_answer", "value": "no", "note": " ".join(core[1:])}
        if core[:3] == ["accept", "and", "proceed"] or core == ["proceed"]:
            return {**base, "intent": "seq_ask", "kind": "proceed"} if self._seq_ready() else None
        if core[:2] in (["record", "defects"], ["record", "defect"]):
            return {**base, "intent": "seq_ask", "kind": "defects", "note": " ".join(core[2:])} if self._seq_ready() else None
        if conf and not pend and self._seq_ready() and not self._running():
            return {**base, "intent": "seq_ask", "kind": "bare_confirm"}
        return None

    # ---- run
    def _seq_ask_run(self, step, board, n, b):
        from .controller import spoken_board
        items = [i for i in seq_items(step) if i["kind"] == "action"]
        tail = f", ends with flashing {self._seq_image_label(step)}" if self._seq_final(step["id"]) and self._seq_image_label(step) else ""
        say = f"Run the {step['id']} sequence on {spoken_board(board)}, {len(items)} actions{tail}. Say confirm."
        return self._seq_pending({"kind": "run", "step": step["id"]}, step, board, n, b, say)

    def _seq_pending(self, seq, step, board, n, b, say):
        timeout = float(self.cfg.get("confirm_timeout_s", 10))
        token = object()
        t = threading.Timer(timeout, self._bench_expire, (token,))
        t.daemon = True
        act = {"id": "sequence", "title": step["title"], "step": step["id"], "section": "_sequence", "params": {}}
        self._bp = {"act": act, "board": board, "text": say, "t0": time.monotonic(), "timeout": timeout, "timer": t, "token": token, "utt": n,
                    "prompt": False, "seq": seq}
        t.start()
        self._changed()
        return {"outcome": "question", "detail": f"confirm {seq['kind']} on {board} within {timeout:g} s", "reply": say, "boards": [board],
                "bench": {**b, "confirm": "asked"}}

    def _seq_confirmed(self, p):
        seq, board, step_id = p["seq"], p["board"], p["act"]["step"]
        b = {"cmd": "confirm", "board": board, "step": step_id, "action": "sequence"}
        sq = self._sq()
        try:
            if seq["kind"] == "run":
                r = self._await(sq.start(step_id, board, True))
                return {"outcome": "applied", "detail": "sequence started", "boards": [board], "bench": {**b, "run": r.get("seq")},
                        "reply": f"Running the {step_id} sequence."}
            if seq["kind"] == "proceed":
                self._await(sq.proceed(step_id, board, bool(seq.get("confirm")), None), 60.0)
                return {"outcome": "applied", "detail": "accepted and proceeding", "boards": [board], "bench": b,
                        "reply": "Accepted." + (" Programming the final image." if self._seq_final(step_id) else "")}
            if seq["kind"] == "defects":
                self._await(sq.defects(step_id, board, seq["note"]), 30.0)
                return {"outcome": "applied", "detail": "defects recorded", "boards": [board], "bench": b, "reply": "Defects recorded. Nothing was flashed."}
        except CmdError as e:
            return self._refused(e, b)
        return {"outcome": "rejected", "detail": "unknown", "reply": "Nothing done."}

    def _seq_final(self, step_id):
        from ..sequence import final_action
        return final_action(self.eng.proto.step(step_id)) is not None

    def _do_seq_ask(self, res, n, text):
        from .controller import spoken_board
        rdy = self._seq_ready()
        b = {"cmd": "seq_" + res["kind"]}
        if not rdy:
            return {"outcome": "rejected", "detail": "no finished run", "reply": "No finished run to accept."}
        board, step, rec = rdy
        rows = self.eng.seq_rows(board, step["id"])
        from ..sequence import verdict_summary
        sm = verdict_summary(rows, self._seq_image_label(step), self._seq_final(step["id"]))
        kind = res["kind"]
        if kind == "bare_confirm":
            if rec["outcome"] != "complete":
                return {"outcome": "rejected", "detail": "run did not complete", "reply": "The run stopped. Record defects, or diagnose.", "bench": b}
            if not sm["ok"]:
                return {"outcome": "rejected", "detail": "failures", "reply": f"{sm['failed']} failed. Record defects, or diagnose.", "bench": b}
            kind = "proceed"
        if kind == "proceed":
            if rec["outcome"] != "complete":
                return {"outcome": "rejected", "detail": "run did not complete", "reply": "The run stopped. Record defects, or diagnose.", "bench": b}
            lab = self._seq_image_label(step)
            say = ("Accept" + ("" if sm["ok"] else f" the {sm['failed']} failed results") + (f" and program {lab}" if self._seq_final(step["id"]) and lab else "")
                   + ". Say confirm.")
            return self._seq_pending({"kind": "proceed", "confirm": not sm["ok"]}, step, board, n, b, say)
        note = (res.get("note") or "").strip()
        if not note:
            return {"outcome": "rejected", "detail": "no note", "reply": "Say record defects, then the note.", "bench": b}
        return self._seq_pending({"kind": "defects", "note": note}, step, board, n, b, f"Record defects on {spoken_board(board)}: {note}. Nothing will be flashed. Say confirm.")

    def _do_seq_answer(self, res, n, text):
        sq = self._sq()
        try:
            self._await(sq.answer(res["value"], res.get("note", ""), "voice"))
        except CmdError as e:
            return {"outcome": "rejected", "detail": str(e), "reply": "No question is waiting."}
        return {"outcome": "applied", "detail": f"operator check: {res['value']}", "reply": "Yes." if res["value"] == "yes" else "No, recorded as a fail."}

    def _do_seq_bench(self, res, n, text):
        sq = self._sq()
        try:
            self._await(sq.bench_reply(res["action"], {}))
        except CmdError as e:
            return {"outcome": "rejected", "detail": str(e), "reply": self._labels(str(e))}
        return {"outcome": "applied", "detail": "bench " + res["action"], "reply": "Starting the protocol." if res["action"] == "confirm" else "Checking again."}

    # ---- speaking the sequencer's events (it runs in the server's loop thread)
    def _seq_listener(self, kind, info):
        if kind == "ask":
            text = info["question"]
        elif kind == "end":
            if info["outcome"] == "complete":
                text = (info["summary"] or {}).get("spoken", "Done.")
            else:
                text = f"The run stopped: {(info.get('stop') or {}).get('reason', info['outcome'])}. Record defects, or diagnose."
        elif kind == "final":
            text = "The final image is programmed." if info["state"] == "ok" else f"The final image was not programmed: {info.get('text') or info['state']}."
        else:
            return
        text = self._labels(text)
        with self._lock:
            self._utt += 1
            out = {"outcome": "applied", "detail": "sequence " + kind, "reply": text, "bench": {"cmd": "sequence", "event": kind}}
            entry = self._record(self._utt, "system", "", {"intent": "sequence_" + kind, "route": "none"}, out, None, None)
        self.tts.say(text)
        self._publish(entry)
