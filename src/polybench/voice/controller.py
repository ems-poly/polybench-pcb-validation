"""VoiceController: applies understood utterances to the Polybench engine and speaks the answer (docs/VOICE.md §3.2-3.4).

One controller per Session. Utterances come from the capture threads or from the routes; `_serial` lets one utterance at a time
through (the local LLM can take seconds), `_lock` guards controller state and every engine call and is never held across the
LLM call or the speech.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any

from .. import engine as eng_mod
from ..engine import CmdError, IN_RANGE, evaluate, expected_text, has_numeric_limit, stop_tripped
from . import config as vcfg
from .benchcmd import BenchCommands, SequenceVoice
from .guide import GuideCommands
from .capture import Capture, INSTALL
from .tts import Tts, is_natural


def _fake_recorder(device):
    from .audio import FakeRecorder
    return FakeRecorder(device)

try:
    from . import understand
except ImportError:                       # NLU module not merged yet: typed text still reaches the journal as 'unclear'
    understand = None

UNIT_WORDS = {"V": "volts", "mV": "millivolts", "A": "amps", "mA": "milliamps", "uA": "microamps", "Hz": "hertz", "kHz": "kilohertz",
              "MHz": "megahertz", "Ohm": "ohms", "kOhm": "kilo ohms", "MOhm": "mega ohms", "mOhm": "milli ohms", "%": "percent"}
CLEAN = ("pass", "none")
ACK_INTENTS = {"accept", "accept_fail", "reject", "cancel", "undo", "skip", "skip_board", "note"}     # replies spoken only with Voice acknowledgements on (UIR-13)


def spoken_board(label: str) -> str:
    return re.sub(r"^([A-Za-z]+)0+(\d)", r"\1\2", label or "")


def unit_words(unit: str | None) -> str:
    return UNIT_WORDS.get(unit or "", unit or "")


_LONG_HEX = re.compile(r"\b(?:0x)?[0-9a-fA-F]{16,}\b")


def spoken_expected(t: dict) -> str:
    """The expected text for the ear: a long hex value (a SHA-256, a UID) is never read out digit by digit (19 Oct, bench session)."""
    s = expected_text(t).replace(">= ", "at least ").replace("<= ", "at most ")
    if _LONG_HEX.search(s):
        s = _LONG_HEX.sub("", s).strip(" ,:;=")
        s = ("matches the build hash " + s).strip() if (t.get("limit") or {}).get("equals") is not None else s
    return re.sub(r"\s{2,}", " ", s).strip()


def _g(x) -> str:
    return f"{x:g}"


def spoken_limit(t: dict) -> str:
    """The limit as the operator sees it (engineering display, the test's own unit), for the ear."""
    lim = t.get("limit") or {}
    lo, hi = lim.get("min"), lim.get("max")
    u = unit_words(t.get("unit") or "")
    tail = f" {u}" if u else ""
    if lo is not None and hi is not None:
        return f"{_g(lo)} to {_g(hi)}{tail}"
    if lo is not None:
        return f"at least {_g(lo)}{tail}"
    return f"at most {_g(hi)}{tail}"


NUM_WORDS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
                                         "sixteen seventeen eighteen nineteen twenty".split())}
YES_W = {"yes", "yeah", "yep", "yup", "affirmative"}
NO_W = {"no", "nope", "negative"}
CANCEL_PHRASES = {"cancel", "cancel that", "never mind", "nevermind", "forget it", "stop"}
FILLER = {"please", "the", "board", "boards", "for", "on", "just", "uh", "um", "okay"}


def _plain(md: str) -> str:
    """Instructions text for the ear: markdown marks removed, one line."""
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md or "")
    t = re.sub(r"[*_`#>]+", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _names(labels: list[str], limit: int = 6) -> str:
    out = [spoken_board(b) for b in labels[:limit]]
    return " ".join(out) + (f" and {len(labels) - limit} more" if len(labels) > limit else "")


def _is_clean(item: dict) -> bool:
    return item["verdict"] in CLEAN and not item["flags"]


def _probe_text(t: dict) -> str:
    """Probes as one spoken sentence: plan format [{point, pole: + | -}] or the older [{net, point}]."""
    pr = []
    for p in t.get("probe") or []:
        if not p.get("point"):
            continue
        if p.get("pole") in ("+", "-"):
            pr.append(f"probe {'plus' if p['pole'] == '+' else 'minus'} on {p['point']}")
        else:
            pr.append(f"{p['net']} at {p['point']}" if p.get("net") else str(p["point"]))
    if not pr:
        return ""
    if not pr[0].startswith("probe "):
        pr[0] = "probe " + pr[0]
    return pr[0][0].upper() + pr[0][1:] + "".join(", " + x for x in pr[1:]) + "."


class VoiceController(SequenceVoice, BenchCommands, GuideCommands):
    def __init__(self, sess):
        self.sess = sess
        self.eng = sess.engine
        self.cfg = vcfg.load()
        self.tts = Tts(lambda: self.cfg)
        self.capture = Capture(lambda: self.cfg, self.tts.speaking, self._on_text, self._on_drop, on_change=self._changed,
                               prompt=self._stt_prompt, cache_dir=str(Path(sess.dir) / "voice-clips"),
                               recorder_factory=_fake_recorder if os.environ.get("POLYBENCH_VOICE_TEST") == "1" else None)
        self._lock = threading.RLock()
        self._serial = threading.Lock()
        self.ctx: dict[str, Any] = {"step": None, "test_id": None, "board": None, "aoi_review": False}
        self.last_reply = ""
        self.last_props: list[dict] = []      # proposals made by the last voice utterance that produced any
        self.pending: dict | None = None      # grace auto-accept
        self.question: dict | None = None     # open range question: {stage: ask | value, test_id, board, need: [boards], text}
        self._sink = self._default_sink
        self._llm_cache: tuple[float, dict] | None = None
        used = [int(e.get("utt") or 0) for e in self.eng.state.voice]
        self._utt = max(used, default=0)
        self._bench_init()
        self._guide_init()
        sq = getattr(sess, "sequencer", None)
        if sq is not None:
            sq.listeners.append(self._seq_listener)

    # ---- plumbing -------------------------------------------------------------------------------------------
    def _default_sink(self, msg: dict):
        import asyncio
        s = self.sess
        if s.loop is not None and s.clients:
            try:
                asyncio.run_coroutine_threadsafe(s.broadcast(msg), s.loop)
            except RuntimeError:
                pass

    def _bc(self, msg: dict):
        self._sink(msg)

    def _changed(self):
        self._bc({"kind": "voice_status"})

    def _nav(self):
        self._bc({"kind": "voice_nav", "step": self.ctx["step"], "test_id": self.ctx["test_id"], "board": self.ctx["board"]})

    def _stt_prompt(self) -> str:
        from . import stt
        st = self.eng.state
        step = self._step()
        first = [t["id"] for t in self._tests(step)] if step else []
        ids = first + [i for i in st.test_index if i not in first]
        return stt.build_prompt(ids, [b["label"] for b in st.boards])

    # ---- context ----------------------------------------------------------------------------------------------
    def set_context(self, step=None, test_id=None, board=None, aoi_review=False):
        with self._lock:
            self.ctx = {"step": step or None, "test_id": test_id or None, "board": board or None, "aoi_review": bool(aoi_review)}

    def _step(self) -> dict | None:
        st = self.eng.state
        sid = self.ctx["step"]
        if not sid and self.ctx["test_id"] in st.test_index:
            sid = st.test_index[self.ctx["test_id"]][0]["id"]
        return self.eng.proto.step(sid) if sid else None

    @staticmethod
    def _tests(step: dict | None) -> list[dict]:
        return [t for sec in (step or {}).get("sections", []) for t in sec.get("tests", [])]

    def _closed_reason(self, board: str, step: dict) -> str:
        st = self.eng.state
        if not st.applies(step, board):
            return f"{step['title']} does not apply to {spoken_board(board)}"
        g = st.gate(step, board)
        if not g["ok"]:
            return f"{spoken_board(board)} is gated, {step['title']} is not open"
        if st.stopped(board):
            return f"{spoken_board(board)} is stopped"
        return ""

    def _open_boards(self, step: dict | None) -> list[str]:
        labels = [b["label"] for b in self.eng.state.boards]
        if step is None:
            return labels
        return [b for b in labels if not self._closed_reason(b, step)]

    def _settled(self, board: str, tid: str) -> bool:
        c = self.eng.state.cell(board, tid)
        return c["status"] in ("accepted", "skipped") or bool(c["proposals"])

    def _next_open(self, tid: str | None, start: str | None, step: dict | None, back: bool = False) -> str | None:
        boards = self._open_boards(step)
        if not boards or not tid:
            return None
        i = boards.index(start) if start in boards else (len(boards) if back else -1)
        order = [boards[(i + (-k if back else k)) % len(boards)] for k in range(1, len(boards) + 1)]
        return next((b for b in order if b != start and not self._settled(b, tid)), None)

    def _view(self):
        st = self.eng.state
        step = self._step()
        tests = []
        for t in self._tests(step):
            tests.append({"id": t["id"], "title": t.get("title", ""), "kind": t["kind"], "unit": t.get("unit"), "expected": expected_text(t),
                          "options": t.get("options", []), "limit": t.get("limit")})
        tid = self.ctx["test_id"]
        boards = self._open_boards(step)
        settled = {b: self._settled(b, tid) for b in boards} if tid in st.test_index else {}
        recent = [f"{e.get('transcript', '')} -> {e.get('reply', '')}" for e in st.voice[-4:]]
        return understand.BenchView(step_id=step["id"] if step else "", tests=tests, boards=boards, all_boards=[b["label"] for b in st.boards],
                                    test_id=tid, board=self.ctx["board"], row_mode=bool(self.cfg["row_mode"]), settled=settled, recent=recent,
                                    step_title=(step or {}).get("title", ""), aoi_review=self._aoi_on())

    # ---- understanding -------------------------------------------------------------------------------------------
    def _interpret(self, text: str, view) -> dict:
        if understand is None:
            return {"transcript": text, "route": "none", "intent": "unclear", "reason": "language understanding module not available"}
        c = self.cfg
        st = self._llm_status()
        try:
            url = vcfg.loopback_only(c["ollama_url"])          # defence in depth: never send a transcript off this Mac
        except ValueError:
            url = None
        # while an action runs the model is not used: a slow model call must never delay the next "stop" (benchcmd.py)
        llm = {"enabled": bool(c["llm_enabled"] and st["ok"] and url and self._running() is None), "model": st["model"], "url": url or "", "timeout_s": 20.0}
        try:
            return understand.interpret(text, view, llm)
        except Exception as e:
            return {"transcript": text, "route": "none", "intent": "unclear", "reason": f"understanding failed: {e}"}

    def handle_text(self, text: str, source: str = "typed", audio: str | None = None, stt_ms: int | None = None) -> dict:
        t0 = time.perf_counter()
        text = (text or "").strip()
        if self.bench_is_stop(text):         # before the utterance lock and any model: a stop never waits (benchcmd.py)
            return self.bench_stop_now(text, source, audio, stt_ms, t0)
        with self._serial:
            with self._lock:
                res = self._bench_route(text, self._bench_base(text)) if text else None
                if res is None and self.question:
                    res = self._range_answer(text)
                view = self._view() if understand is not None and res is None else None
            if res is None:
                res = self._interpret(text, view) if text else {"transcript": text, "route": "none", "intent": "unclear", "reason": "empty"}
                with self._lock:
                    res = self._range_value_stage(res, text)
            with self._lock:
                self._utt += 1
                out = self._apply(res, self._utt, text)
                self._bench_after(res, out)
                self._guide_after(res, out)
                entry = self._record(self._utt, source, text, res, out, audio, stt_ms)
            self.tts.say(out["reply"], kind="ack" if res.get("intent") in ACK_INTENTS else "readout")
            if out.get("grace"):
                self._arm(entry["utt"], out["grace"])
            if out.get("aoi_grace"):
                self._arm(entry["utt"], [], aoi=out["aoi_grace"])
            self._publish(entry, nav=out.get("nav", False))
            return entry

    def _record(self, utt, source, text, res, out, audio, stt_ms) -> dict:
        llm = res.get("llm") or {}
        payload = {"utt": utt, "source": source, "transcript": text, "route": res.get("route", "none"), "model": res.get("model"),
                   "intent": res.get("intent", "unclear"), "values": out.get("values", []),
                   "boards": out.get("boards") or list(res.get("boards") or []), "test_id": self.ctx["test_id"], "step": self.ctx["step"],
                   "board": self.ctx["board"], "outcome": out["outcome"], "detail": out.get("detail", ""), "reply": out["reply"],
                   "stt_ms": stt_ms, "llm_ms": llm.get("ms"), "audio": audio}
        if out.get("bench"):
            payload["bench"] = out["bench"]
        if out.get("guide"):
            payload["guide"] = out["guide"]
        self.eng._emit("voice", payload)
        self.last_reply = out["reply"] if res.get("intent") != "repeat" else self.last_reply
        return self.eng.state.voice[-1]

    def _publish(self, entry: dict, nav: bool = False):
        if nav:
            self._nav()
        self._bc({"kind": "voice", "entry": entry})
        self._bc({"kind": "state"})
        self._changed()

    # ---- capture callbacks -------------------------------------------------------------------------------------
    def _on_text(self, text: str, source: str, info: dict) -> dict:
        return self.handle_text(text, source=source, audio=info.get("audio"), stt_ms=info.get("stt_ms"))

    def _on_drop(self, why: str, text, source: str, info: dict) -> dict:
        if why.startswith(("noise", "dropped: own readout")):   # a Whisper hallucination, or our own readout heard (UIR-37): logged, never interpreted, never spoken (speaking it would feed the loop)
            with self._lock:
                self._utt += 1
                entry = self._record(self._utt, source, text or "", {"intent": "unclear", "route": "none"},
                                     {"outcome": "dropped", "detail": "dropped: own readout" if why.startswith("dropped: own") else "dropped: noise", "reply": ""}, info.get("audio"), info.get("stt_ms"))
            self._publish(entry)
            return entry
        reply = f"Nothing used: {why.split(':')[0]}" + (f" (heard {text})" if text else "")
        with self._lock:
            self._utt += 1
            entry_out = {"outcome": "unclear", "detail": why, "reply": reply}
            entry = self._record(self._utt, source, text or "", {"intent": "unclear", "route": "none"}, entry_out, info.get("audio"), info.get("stt_ms"))
        self.tts.say(reply)
        self._publish(entry)
        return entry

    # ---- applying an interpretation ---------------------------------------------------------------------------------
    def _apply(self, res: dict, n: int, text: str) -> dict:
        intent = res.get("intent", "unclear")
        if intent in ("next_test", "prev_test", "goto_test", "set_board", "next_board", "prev_board", "skip", "skip_board"):
            self._recent = None
        fn = getattr(self, "_do_" + intent, None)
        if fn is None:
            fn = self._do_unclear
        try:
            return fn(res, n, text)
        except CmdError as e:
            return {"outcome": "rejected", "detail": str(e), "reply": f"Refused: {e}"}

    def _examples(self) -> list[str]:
        """Two phrases that are valid in the current step, for the hint after an utterance that was not understood."""
        if self._aoi_on():
            return ["confirm", "not an issue"]
        st = self.eng.state
        t = st.test_index[self.ctx["test_id"]][2] if self.ctx["test_id"] in st.test_index else None
        if t is None:
            return ["which test", "next"]
        kind = t.get("kind")
        if kind == "yn":
            return ["S one yes", "next"]
        if kind == "choice" and t.get("options"):
            return [f"S one {t['options'][0]}", "next"]
        if kind == "number":
            u = unit_words(t.get("unit") or "")
            return [f"S one 3.3 {u}".strip(), "next"]
        return ["which test", "next"]

    def _do_unclear(self, res, n, text):
        why = res.get("reason") or "not understood"
        if not text:
            return {"outcome": "unclear", "detail": why, "reply": "Nothing heard."}
        q = self._range_question()
        if q:
            self.question = q
            return {"outcome": "question", "detail": why, "reply": q["text"], "boards": [q["board"]]}
        a, b = self._examples()
        return {"outcome": "unclear", "detail": why, "reply": f"Not understood. Try: {a}, or {b}."}

    def _no_test(self) -> str:
        step = self._step()
        return f"No test selected. You are in {step['title']}." if step else "No test selected."

    def _do_mic_check(self, res, n, text):
        step = self._step()
        tid = self.ctx["test_id"]
        where = step["title"] if step else "no step"
        what = tid if tid in self.eng.state.test_index else "no test selected"
        a = self._examples()[0]
        return {"outcome": "applied", "detail": "mic check", "reply": f"I can hear you. You are in {where}, {what}. Try: {a}."}

    def _source(self, res, n, text) -> dict:
        return {"kind": "voice", "key": "voice", "component": "voice", "input_method": "voice", "route": res.get("route"),
                "model": res.get("model"), "utterance": n, "transcript": text, "operator": self.eng.operator}

    def _do_values(self, res, n, text):
        self._flush_grace("moved on")
        src = self._source(res, n, text)
        origin = "simulated" if self.eng.simulated else "measured"
        made, refused, vals = [], [], []
        for v in res.get("values") or []:
            b, tid = v.get("board"), v.get("test_id") or self.ctx["test_id"]
            rec = {"board": b, "test_id": tid, "value": v.get("value"), "unit": v.get("unit"), "heard": v.get("heard")}
            try:
                if tid not in self.eng.state.test_index:
                    raise CmdError("no test selected" if not tid else f"unknown test {tid}")
                s, sec, t = self.eng.state.test_index[tid]
                self.eng._board(b)
                why = self._closed_reason(b, s)
                if why:
                    raise CmdError(why)
                ev = evaluate(t, str(v.get("value")))
                if not ev["ok"]:
                    raise CmdError(ev["error"])
                if self.eng.state.cell(b, tid)["status"] == "accepted":
                    raise CmdError(f"{spoken_board(b)} already has a value for {tid}")
                pev = self.eng.add_proposal(b, tid, str(v["value"]), v.get("unit") or None, raw=v.get("heard"), source=src, origin=origin)
                p = pev["payload"]
                flags = list(p["flags"]) + (["stop_rule"] if stop_tripped(t, ev) else [])      # a stop-rule value is held like a FAIL
                item = {"board": b, "test_id": tid, "id": pev["id"], "verdict": p["verdict"], "flags": flags, "value": p["value"],
                        "unit": p.get("unit"), "reason": p.get("reason", ""), "test": t}
                made.append(item)
                vals.append({**rec, "verdict": p["verdict"], "flags": p["flags"], "proposal_id": pev["id"], "status": "proposed"})
            except CmdError as e:
                refused.append((b, str(e)))
                vals.append({**rec, "status": "refused", "reason": str(e)})
        out: dict = {"values": vals, "boards": [x["board"] for x in made]}
        if not made:
            why = refused[0][1] if refused else (res.get("reason") or "no value heard")
            out.update(outcome="rejected", detail=why, reply=("Nothing recorded: " + why) if refused else f"No value: {why}")
            return out
        self.last_props = made
        out["reply"] = self._read_back(made)
        clean = [m for m in made if _is_clean(m)]
        held = [m for m in made if not _is_clean(m)]
        if refused:
            out["reply"] = f"{out['reply'].rsplit('. Say', 1)[0]}. Not taken: " + "; ".join(f"{spoken_board(b)} {w}" for b, w in refused[:3]) + ". " + \
                self._tail(clean, held)
        # row mode: a value on the current board moves the cursor to the next open board
        if self.cfg["row_mode"] and len(made) == 1 and made[0]["board"] == self.ctx["board"]:
            nxt = self._next_open(made[0]["test_id"], made[0]["board"], self._step())
            if nxt:
                self._recent = {"test_id": made[0]["test_id"], "board": made[0]["board"], "utt": self._utt}
                self.ctx["board"] = nxt
                out["nav"] = True
                head, tail = out["reply"].rsplit(" Say", 1) if " Say" in out["reply"] else (out["reply"], "")
                out["reply"] = f"{head} Next {spoken_board(nxt)}." + (" Say" + tail if tail else "")
        out.update(outcome="proposed", detail=f"{len(made)} proposed" + (f", {len(refused)} refused" if refused else ""))
        rq = res.get("_rq")
        if rq and any(m["board"] == rq["need"][0] for m in made):       # a reading asked for after "no": ask for the next named board
            rest = rq["need"][1:]
            if rest:
                self.question = {**rq, "need": rest, "board": rest[0]}
                self.ctx["board"] = rest[0]
                out["nav"] = True
                out["reply"] += f" Say the reading for {spoken_board(rest[0])}."
        if clean and self.cfg["grace_s"] > 0:
            out["grace"] = clean
        return out

    @staticmethod
    def _tail(clean, held) -> str:
        if clean and held:
            return "Say accept, accept fail, or reject."
        return "Say accept fail, or reject." if held else "Say next test to accept, or reject."

    def _item_words(self, m: dict) -> str:
        t = m["test"]
        if m.get("range"):
            return f"{spoken_board(m['board'])} in range, no reading."
        val = f"{m['value']} {unit_words(m['unit'])}".strip()
        sb = spoken_board(m["board"])
        exp = spoken_expected(t)
        if m["verdict"] == "fail":
            return f"{sb} {val} is outside the limit: say accept fail, or reject." if t["kind"] in ("number", "hex") and exp \
                else f"{sb} {val} is a FAIL, {m['reason'] or 'failed'}."
        if m["verdict"] == "review":
            return f"{sb} {val} is outside the estimated limit {exp}: review."
        if "outside_tbr" in m["flags"]:
            return f"{sb} {val} is outside the TBR limit {exp}, which cannot fail."
        return f"{sb} {val}."

    def _read_back(self, made: list[dict]) -> str:
        clean = [m for m in made if _is_clean(m)]
        held = [m for m in made if not _is_clean(m)]
        if made and all(m.get("range") for m in made) and len(made) > 1:
            return (f"{len(made)} boards in range, no reading: {_names([m['board'] for m in made])}"
                    + f". {self._tail(clean, held)}")
        if len(made) == 1:
            w = self._item_words(made[0])
            return w if held and "say accept fail" in w else f"{w} {self._tail(clean, held)}"
        groups = {"pass": [], "fail": [], "review": [], "tbr": [], "rec": []}
        for m in made:
            k = m["verdict"] if m["verdict"] in ("pass", "fail", "review") else ("tbr" if "outside_tbr" in m["flags"] else "rec")
            groups[k].append(m["board"])
        parts = []
        if groups["pass"]:
            parts.append(f"{len(groups['pass'])} pass")
        for k, label in (("fail", "fail"), ("review", "review"), ("tbr", "outside the TBR limit"), ("rec", "recorded")):
            if groups[k]:
                parts.append(f"{len(groups[k])} {label}" + (f" on {_names(groups[k])}" if k != "rec" else ""))
        same = len({(m["value"], m["unit"]) for m in made}) == 1
        head = f"{len(made)} boards, {made[0]['value']} {unit_words(made[0]['unit'])}".strip() if same else f"{len(made)} values"
        return f"{head}: {', '.join(parts)}. {self._tail(clean, held)}"

    # ---- range question: a number test, an utterance that was not understood ---------------------------------------------
    def _range_question(self) -> dict | None:
        """The question to ask instead of the generic hint, or None (no test, AOI review, no numeric limit, no open board)."""
        st = self.eng.state
        tid = self.ctx["test_id"]
        if self._aoi_on() or tid not in st.test_index:
            return None
        s, sec, t = st.test_index[tid]
        if not has_numeric_limit(t):
            return None

        def ok(b):
            return (not self._closed_reason(b, s) and st.sec_applies(s, sec, b) and st.cell(b, tid)["status"] not in ("accepted", "skipped"))
        cur = self.ctx["board"]
        b = cur if cur and ok(cur) else next((x for x in self._open_boards(s) if ok(x) and not self._settled(x, tid)), None)
        if not b:
            return None
        return {"stage": "ask", "test_id": tid, "board": b, "need": [], "text": f"Is the reading {spoken_limit(t)} on {spoken_board(b)}?"}

    def _board_names(self, words: list[str]) -> list[str] | None:
        """Board labels named by words like 's 2 and s 3', or None when any of the words is not a board."""
        ws = [str(NUM_WORDS[w]) if w in NUM_WORDS else w for w in words if w != "and"]
        txt = " ".join(ws)
        if not txt or not re.fullmatch(r"(?:\s*[a-z]+\s*0*\d+)+", txt):
            return None
        known = {spoken_board(b["label"]).lower(): b["label"] for b in self.eng.state.boards}
        out = []
        for pre, n in re.findall(r"([a-z]+)\s*0*(\d+)", txt):
            lab = known.get(f"{pre}{n}")
            if not lab:
                return None
            if lab not in out:
                out.append(lab)
        return out

    def _range_answer(self, text: str) -> dict | None:
        """Called with a question open. -> an interpretation when the text answers it, else None (the question is dropped,
        except while it waits for a reading, where the normal interpretation runs with the cursor on that board)."""
        q = self.question
        toks, _ = (re.findall(r"[a-z0-9]+", (text or "").lower().replace("'", "")), None)
        base = {"transcript": text, "route": "grammar", "model": None, "values": [], "boards": [], "test_id": q["test_id"], "text": "",
                "reason": None, "grammar": {"ok": True, "reason": None, "fields": None}, "llm": None}
        if self.ctx["test_id"] != q["test_id"]:
            self.question = None
            return None
        if " ".join(toks) in CANCEL_PHRASES:
            return {**base, "intent": "range_cancel"}
        if q["stage"] == "value":
            self.ctx["board"] = q["need"][0]
            return None
        words = [w for w in toks if w not in FILLER]
        ans = None
        if words and words[0] in YES_W | NO_W | {"only"}:
            head, rest = words[0], words[1:]
            if head in YES_W and rest in ([], ["all"], ["everywhere"], ["every", "one"]):
                ans = ("yes", "all" if rest else None)
            elif head in YES_W | {"only"} | NO_W and rest:
                bs = self._board_names(rest)
                if bs:
                    ans = ("no" if head in NO_W else "yes", bs)
            elif head in YES_W | NO_W and not rest:
                ans = ("no" if head in NO_W else "yes", None)
        if ans is None:
            self.question = None
            return None
        return {**base, "intent": "range_" + ans[0], "boards": ans[1] if isinstance(ans[1], list) else [], "all": ans[1] == "all"}

    def _range_value_stage(self, res: dict, text: str) -> dict:
        """After interpretation, with a reading asked for: a value is taken for the asked board; an unclear text asks again."""
        q = self.question
        if not q or q["stage"] != "value":
            return res
        if res.get("intent") == "values":
            self.question = None
            res["_rq"] = q
            self._nav()
            return res
        if res.get("intent") == "unclear" and text:
            res = {**res, "intent": "range_again"}
            return res
        self.question = None
        return res

    def _do_range_again(self, res, n, text):
        q = self.question
        return {"outcome": "question", "detail": res.get("reason") or "no reading heard", "boards": [q["need"][0]],
                "reply": f"Say the reading for {spoken_board(q['need'][0])}, or cancel."}

    def _do_range_cancel(self, res, n, text):
        self.question = None
        return {"outcome": "applied", "detail": "range question cancelled", "reply": "Cancelled. Nothing recorded."}

    def _do_range_no(self, res, n, text):
        q = self.question
        tid = q["test_id"]
        s, sec, t = self.eng.state.test_index[tid]
        boards = list(res.get("boards") or [q["board"]])
        ok, errs = [], []
        for b in boards:
            why = self._range_refusal(b, s, sec, tid)
            (errs.append((b, why)) if why else ok.append(b))
        if not ok:
            self.question = None
            why = errs[0][1]
            return {"outcome": "rejected", "detail": why, "reply": f"Nothing recorded: {why}"}
        self.question = {**q, "stage": "value", "need": ok, "board": ok[0]}
        self.ctx["board"] = ok[0]
        reply = f"Say the reading for {spoken_board(ok[0])}."
        if errs:
            reply = "Not taken: " + "; ".join(f"{spoken_board(b)} {w}" for b, w in errs[:3]) + ". " + reply
        return {"outcome": "question", "detail": "reading asked for", "reply": reply, "boards": ok, "nav": True}

    def _range_refusal(self, b: str, s: dict, sec: dict, tid: str) -> str:
        self.eng._board(b)
        why = self._closed_reason(b, s)
        if why:
            return why
        if not self.eng.state.sec_applies(s, sec, b):
            return f"{tid} does not apply to {spoken_board(b)}"
        if self.eng.state.cell(b, tid)["status"] == "accepted":
            return f"{spoken_board(b)} already has a value for {tid}"
        return ""

    def _do_range_yes(self, res, n, text):
        q = self.question
        self.question = None
        self._flush_grace("moved on")
        tid = q["test_id"]
        s, sec, t = self.eng.state.test_index[tid]
        refused = []
        if res.get("all"):
            boards = [b for b in self.eng.batch_targets(tid)["targets"] if not self._settled(b, tid)]
            if not boards:
                return {"outcome": "rejected", "detail": "no empty board", "reply": f"Nothing recorded: every board already has a value for {tid}."}
        else:
            boards = []
            for b in (res.get("boards") or [q["board"]]):
                why = self._range_refusal(b, s, sec, tid)
                (refused.append((b, why)) if why else boards.append(b))
            if not boards:
                return {"outcome": "rejected", "detail": refused[0][1], "reply": f"Nothing recorded: {refused[0][1]}"}
        src = self._source(res, n, text)
        made, vals = [], []
        for b in boards:
            pev = self.eng.add_proposal(b, tid, IN_RANGE, t.get("unit") or None, raw=text, source=src, origin="operator_confirmed",
                                        via="voice_range", question=q["text"])
            p = pev["payload"]
            made.append({"board": b, "test_id": tid, "id": pev["id"], "verdict": p["verdict"], "flags": p["flags"], "value": IN_RANGE,
                         "unit": p.get("unit"), "reason": "", "test": t, "range": True})
            vals.append({"board": b, "test_id": tid, "value": IN_RANGE, "unit": p.get("unit"), "heard": text, "verdict": p["verdict"],
                         "flags": p["flags"], "proposal_id": pev["id"], "status": "proposed"})
        for b, why in refused:
            vals.append({"board": b, "test_id": tid, "status": "refused", "reason": why})
        self.last_props = made
        out: dict = {"values": vals, "boards": boards, "reply": self._read_back(made)}
        if refused:
            out["reply"] = f"{out['reply'].rsplit('. Say', 1)[0]}. Not taken: " + "; ".join(f"{spoken_board(b)} {w}" for b, w in refused[:3]) + ". " + \
                self._tail([m for m in made if _is_clean(m)], [m for m in made if not _is_clean(m)])
        if self.cfg["row_mode"] and not res.get("all") and len(made) == 1 and made[0]["board"] == self.ctx["board"]:
            nxt = self._next_open(tid, made[0]["board"], self._step())
            if nxt:
                self.ctx["board"] = nxt
                out["nav"] = True
                head, tail = out["reply"].rsplit(" Say", 1) if " Say" in out["reply"] else (out["reply"], "")
                out["reply"] = f"{head} Next {spoken_board(nxt)}." + (" Say" + tail if tail else "")
        out.update(outcome="proposed", detail=f"{len(made)} range confirmed, no reading")
        clean = [m for m in made if _is_clean(m)]
        if clean and self.cfg["grace_s"] > 0:
            out["grace"] = clean
        return out

    # ---- accept / reject ----------------------------------------------------------------------------------------------
    def _live(self) -> list[dict]:
        out = []
        for it in self.last_props:
            c = self.eng.state.cell(it["board"], it["test_id"])
            if c["status"] != "accepted" and any(q["id"] == it["id"] for q in c["proposals"].values()):
                out.append(it)
        return out

    def _take(self, items: list[dict], confirm: bool) -> tuple[list[dict], list[tuple], list[str]]:
        ok, errs, stops = [], [], []
        for it in items:
            try:
                r = self.eng.accept(it["board"], it["test_id"], it["id"], confirm=confirm)
                ok.append(it)
                if r.get("stop"):
                    stops.append(f"Stop on {spoken_board(it['board'])}: {r['stop']}.")
            except CmdError as e:
                errs.append((it["board"], str(e)))
        return ok, errs, stops

    def _do_accept(self, res, n, text):
        self._drop_pending()
        live = self._live()
        if not live:
            return {"outcome": "rejected", "detail": "nothing pending", "reply": "Nothing to accept."}
        clean = [i for i in live if _is_clean(i)]
        held = [i for i in live if not _is_clean(i)]
        ok, errs, stops = self._take(clean, False)
        return self._accept_reply(ok, errs, stops, held, "accept")

    def _accept_reply(self, ok, errs, stops, held, what) -> dict:
        parts = []
        if ok:
            parts.append("Accepted." if len(ok) == 1 else f"Accepted {len(ok)}.")
        if held:
            if len(held) == 1 and not ok:
                parts.append(self._item_words(held[0]) if "say accept fail" in self._item_words(held[0]) else f"{self._item_words(held[0])} Say accept fail, or reject.")
            else:
                allfail = all(h["verdict"] == "fail" for h in held)
                parts.append(f"{len(held)} {'fails ' if allfail else ''}held: say accept fail or reject.")
        for b, w in errs[:3]:
            parts.append(f"Could not accept {spoken_board(b)}: {w}.")
        parts += stops
        outcome = "applied" if ok else "rejected"
        return {"outcome": outcome, "detail": f"{len(ok)} accepted, {len(held)} held, {len(errs)} errors",
                "reply": " ".join(parts) or "Nothing to accept.", "boards": [i["board"] for i in ok]}

    def _do_accept_fail(self, res, n, text):
        self._drop_pending()
        live = self._live()
        held = [i for i in live if not _is_clean(i)]
        if not held:
            return {"outcome": "rejected", "detail": "nothing held",
                    "reply": "Nothing is held. Say accept." if live else "Nothing to accept."}
        ok, errs, stops = self._take(held, True)
        parts = []
        if ok:
            parts.append(f"Accepted fail on {_names([i['board'] for i in ok])}." if len(ok) <= 3 else f"Accepted {len(ok)} fails.")
        for b, w in errs[:3]:
            parts.append(f"Could not accept {spoken_board(b)}: {w}.")
        rest = [i for i in self._live() if _is_clean(i)]
        if rest:
            parts.append(f"{len(rest)} clean still pending: say accept.")
        parts += stops
        return {"outcome": "applied" if ok else "rejected", "detail": f"{len(ok)} accepted with confirm", "reply": " ".join(parts),
                "boards": [i["board"] for i in ok]}

    def _do_reject(self, res, n, text, word="Rejected"):
        self._drop_pending()
        live = self._live()
        if not live:
            return {"outcome": "rejected", "detail": "nothing pending", "reply": f"Nothing to {'undo' if word == 'Undone' else 'reject'}."}
        for it in live:
            self.eng.reject(it["board"], it["test_id"], it["id"], "voice")
        self.last_props = []
        return {"outcome": "applied", "detail": f"{len(live)} rejected", "boards": [i["board"] for i in live],
                "reply": f"{word}."}

    def _do_undo(self, res, n, text):
        out = self._do_reject(res, n, text, "Undone")
        if out["outcome"] == "rejected":
            out["reply"] = "Nothing to undo. Accepted values are edited on the screen."
        return out

    def _do_cancel(self, res, n, text):
        if self.pending and self.pending.get("aoi"):
            self._drop_pending()
            return {"outcome": "applied", "detail": "grace cancelled", "reply": "Cancelled. The issue stays undecided."}
        if self._live():
            out = self._do_reject(res, n, text)
            out["detail"] = "cancelled: " + out["detail"]
            return out
        self._drop_pending()
        return {"outcome": "rejected", "detail": "nothing to cancel", "reply": "Nothing to cancel."}

    # ---- grace auto-accept ---------------------------------------------------------------------------------------------
    def _arm(self, utt: int, items: list[dict], aoi: dict | None = None):
        with self._lock:
            self._drop_pending()
            self.pending = {"utt": utt, "items": items, "armed": False, "t0": None, "timer": None, "aoi": aoi}
        self.tts.when_done(lambda: self._start_timer(utt))
        self._changed()

    def _start_timer(self, utt: int, delay: float | None = None):
        with self._lock:
            p = self.pending
            if not p or p["utt"] != utt:
                return
            p["armed"], p["t0"] = True, time.monotonic()
            p["timer"] = threading.Timer(self.cfg["grace_s"] if delay is None else delay, self._fire, (utt,))
            p["timer"].daemon = True
            p["timer"].start()
        self._changed()

    def _drop_pending(self):
        p = self.pending
        if p and p.get("timer"):
            p["timer"].cancel()
        self.pending = None

    def cancel_pending(self) -> dict:
        with self._lock:
            had = self.pending is not None or self._bp is not None
            self._drop_pending()
            self._bench_drop("cancelled")
        if had:
            self._changed()
        return self.status()

    def _busy_hearing(self) -> bool:
        c = self.capture
        return c.recording or c.busy or c.seg.active or self.tts.speaking()

    def _fire(self, utt: int):
        with self._lock:
            p = self.pending
            if not p or p["utt"] != utt:
                return
            if self._busy_hearing():             # the operator is speaking (probably "reject"): wait for the utterance to land
                self._start_timer(utt, 0.3)
                return
            self.pending = None
            if p.get("aoi"):
                try:
                    self._aoi_apply(p["aoi"])
                    out = {"outcome": "applied", "detail": f"grace period of {self.cfg['grace_s']:g} s passed: {p['aoi']['key']} {p['aoi']['decision']}",
                           "reply": ""}
                except CmdError as e:
                    out = {"outcome": "rejected", "detail": str(e), "reply": f"Refused: {e}"}
                self._utt += 1
                entry = self._record(self._utt, "grace", "", {"intent": "aoi_" + p["aoi"]["decision"], "route": "none"}, out, None, None)
                self._publish(entry)
                return
            live = [i for i in p["items"] if i in self._live() and _is_clean(i)]
            if not live:
                return
            ok, errs, stops = self._take(live, False)
            out = self._accept_reply(ok, errs, stops, [], "grace")
            out["detail"] = f"grace period of {self.cfg['grace_s']:g} s passed: " + out["detail"]
            self._utt += 1
            entry = self._record(self._utt, "grace", "", {"intent": "accept", "route": "none"}, out, None, None)
        self.tts.say(out["reply"], kind="ack")
        self._publish(entry)

    def _flush_grace(self, why: str):
        """The operator carried on with new values without reject/cancel: the earlier clean ones count as accepted."""
        p = self.pending
        if not p:
            return
        self._drop_pending()
        live = [i for i in p["items"] if i in self._live() and _is_clean(i)]
        self._take(live, False)

    # ---- skip, navigation ---------------------------------------------------------------------------------------------
    def _do_skip_board(self, res, n, text):
        boards = list(res.get("boards") or ([self.ctx["board"]] if self.ctx["board"] else []))
        tid = res.get("test_id") or self.ctx["test_id"]
        reason = (res.get("text") or "").strip()
        if not boards or not tid:
            return {"outcome": "rejected", "detail": "no board or test", "reply": "Which board and test to skip?"}
        if not reason:
            return {"outcome": "rejected", "detail": "a skip needs a reason", "boards": boards,
                    "reply": f"Skip {_names(boards)} on {tid}: say the reason. Nothing recorded."}
        done, errs = [], []
        for b in boards:
            try:
                self.eng.skip(b, tid, reason)
                done.append(b)
            except CmdError as e:
                errs.append((b, str(e)))
        out: dict = {"boards": done, "outcome": "applied" if done else "rejected", "detail": f"skipped {len(done)}: {reason}"}
        parts = [f"Skipped {_names(done)} on {tid}: {reason}."] if done else []
        parts += [f"Could not skip {spoken_board(b)}: {w}." for b, w in errs[:3]]
        if done and self.cfg["row_mode"] and self.ctx["board"] in done:
            nxt = self._next_open(tid, self.ctx["board"], self._step())
            if nxt:
                self.ctx["board"] = nxt
                out["nav"] = True
                parts.append(f"Next {spoken_board(nxt)}.")
        out["reply"] = " ".join(parts)
        return out

    def _do_skip(self, res, n, text):
        step = self._step()
        tid = self.ctx["test_id"]
        if self.cfg["row_mode"] and tid:
            nxt = self._next_open(tid, self.ctx["board"], step)
            if nxt:
                self.ctx["board"] = nxt
                return {"outcome": "applied", "detail": "moved on, nothing recorded", "reply": f"{spoken_board(nxt)}.", "nav": True}
        return self._step_test(1)

    def _step_test(self, d: int) -> dict:
        step = self._step()
        tests = self._tests(step)
        if not tests:
            return {"outcome": "rejected", "detail": "no tests here", "reply": "No tests on this page."}
        ids = [t["id"] for t in tests]
        i = ids.index(self.ctx["test_id"]) + d if self.ctx["test_id"] in ids else (0 if d > 0 else len(ids) - 1)
        if not 0 <= i < len(ids):
            return {"outcome": "rejected", "detail": "end of step", "reply": "Last test in this step." if d > 0 else "First test in this step."}
        return self._goto(ids[i])

    def _goto(self, tid: str) -> dict:
        st = self.eng.state
        if tid not in st.test_index:
            return {"outcome": "rejected", "detail": f"unknown test {tid}", "reply": f"No test {tid}."}
        s, sec, t = st.test_index[tid]
        self.ctx["step"], self.ctx["test_id"] = s["id"], tid
        reply = f"{tid}, {t.get('title', '')}."
        if self.cfg["row_mode"]:
            first = next((x for x in self._open_boards(s) if not self._settled(x, tid)), None)
            if first:
                self.ctx["board"] = first
                reply += f" Board {spoken_board(first)}."
        return {"outcome": "applied", "detail": f"test {tid}", "reply": reply, "nav": True}

    def _do_next_test(self, res, n, text):
        live = self._live()
        if live:
            if any(not _is_clean(i) for i in live):
                return {"outcome": "rejected", "detail": "pending fail blocks next test",
                        "reply": "Pending fail: say accept fail or reject first."}
            self._drop_pending()
            ok, errs, stops = self._take(live, False)
            out = self._step_test(1)
            if errs or stops:
                out["reply"] = " ".join([f"Could not accept {spoken_board(b)}: {w}." for b, w in errs[:3]] + stops + [out["reply"]])
            out["boards"] = [i["board"] for i in ok]
            return out
        return self._step_test(1)

    def _do_prev_test(self, res, n, text):
        return self._step_test(-1)

    def _do_goto_test(self, res, n, text):
        return self._goto(res.get("test_id") or "")

    def _do_set_board(self, res, n, text):
        bs = list(res.get("boards") or [])
        if not bs:
            return {"outcome": "rejected", "detail": "no board", "reply": "Which board?"}
        self.eng._board(bs[0])
        self.ctx["board"] = bs[0]
        return {"outcome": "applied", "detail": f"board {bs[0]}", "reply": f"{spoken_board(bs[0])}.", "nav": True, "boards": [bs[0]]}

    def _do_next_board(self, res, n, text, back=False):
        nxt = self._next_open(self.ctx["test_id"], self.ctx["board"], self._step(), back=back)
        if not nxt:
            return {"outcome": "rejected", "detail": "no open board", "reply": f"Every board has a value for {self.ctx['test_id'] or 'this test'}."}
        self.ctx["board"] = nxt
        return {"outcome": "applied", "detail": f"board {nxt}", "reply": f"{spoken_board(nxt)}.", "nav": True, "boards": [nxt]}

    def _do_prev_board(self, res, n, text):
        return self._do_next_board(res, n, text, back=True)

    # ---- notes and questions --------------------------------------------------------------------------------------------
    def _do_note(self, res, n, text):
        body = (res.get("text") or text).strip()
        tid = res.get("test_id") or self.ctx["test_id"] or ""
        board = self.ctx["board"]
        rc = getattr(self, "_recent", None)     # row mode moved on after a value: a note said right after it belongs to that board (PoC recent_row)
        if rc and rc["test_id"] == tid and self._utt - rc["utt"] <= 3:
            board = rc["board"]
        boards = list(res.get("boards") or ([board] if board else []))
        known = {d.upper() for d in self.eng.state.bom_designators()}
        des = list(dict.fromkeys(m.upper() for m in re.findall(r"\b[A-Za-z]{1,3}\d{1,3}[A-Za-z]?\b", body) if m.upper() in known))
        step = self._step()
        r = self.eng.note(body, boards[0] if boards else "", tid, step=step["id"] if step else "", boards=boards, designators=des)
        return {"outcome": "applied", "detail": f"note {r['id']}", "boards": boards,
                "reply": "Note saved" + (f" on {' '.join(des)}" if des else "") + "."}

    def _do_which_test(self, res, n, text):
        tid = res.get("test_id") or self.ctx["test_id"]
        if tid not in self.eng.state.test_index:
            return {"outcome": "rejected", "detail": "no test", "reply": self._no_test()}
        t = self.eng.state.test_index[tid][2]
        parts = [f"{tid}, {t.get('title', '')}."]
        pt = _probe_text(t)
        if pt:
            parts.append(pt)
        exp = spoken_expected(t)
        if exp:
            parts.append(f"Expected {exp}.")
        return {"outcome": "applied", "detail": tid, "reply": " ".join(parts)}

    def _do_progress(self, res, n, text):
        tid = self.ctx["test_id"]
        step = self._step()
        if tid not in self.eng.state.test_index or step is None:
            return {"outcome": "rejected", "detail": "no test", "reply": self._no_test()}
        boards = [b["label"] for b in self.eng.state.boards if self.eng.state.applies(step, b["label"])]
        done = [b for b in boards if self.eng.state.cell(b, tid)["status"] in ("accepted", "skipped")]
        left = [b for b in boards if b not in done]
        if not left:
            return {"outcome": "applied", "detail": "all done", "reply": f"{tid}: all {len(boards)} boards done."}
        return {"outcome": "applied", "detail": f"{len(done)}/{len(boards)}",
                "reply": f"{tid}: {len(done)} of {len(boards)} boards done, {len(left)} left, next {spoken_board(left[0])}."}

    def _do_read_method(self, res, n, text):
        tid = res.get("test_id") or self.ctx["test_id"]
        if tid not in self.eng.state.test_index:
            return {"outcome": "rejected", "detail": "no test", "reply": self._no_test()}
        step, sec, t = self.eng.state.test_index[tid]
        parts = [f"{tid}, {t.get('title', '')}."]
        ins = _plain(sec.get("instructions", ""))
        if ins:
            parts.append(ins if ins[-1] in ".!?" else ins + ".")
        pt = _probe_text(t)
        if pt:
            parts.append(pt)
        exp = spoken_expected(t)
        if exp:
            parts.append(f"Expected {exp}.")
        si = t.get("stop_if")
        if si:
            unit = f" {t['unit']}" if t.get("unit") else ""
            def _v(x):
                if isinstance(x, (list, tuple)):
                    return " to ".join(_v(y) for y in x)
                return f"{x:g}{unit}" if isinstance(x, (int, float)) else str(x)
            cond = next((f"{w} {_v(si[k])}" for k, w in (("below", "below"), ("above", "above"), ("equals", "equal to"), ("outside", "outside"))
                         if k in si), "")
            parts.append(f"Stop {cond}: {si.get('text', '').rstrip('.')}." if cond else f"Stop rule: {si.get('text', '')}")
        return {"outcome": "applied", "detail": f"method {tid}", "reply": " ".join(parts)}

    def _do_highlight(self, res, n, text):
        tid = res.get("test_id") or self.ctx["test_id"]
        if tid not in self.eng.state.test_index:
            return {"outcome": "rejected", "detail": "no test", "reply": self._no_test()}
        t = self.eng.state.test_index[tid][2]
        sess = self.sess
        try:
            fn = getattr(sess, "highlight_test", None)
            if fn is not None:
                r = fn(t)
            else:
                r = sess.highlight(list(t.get("highlight", [])) + [p["point"] for p in t.get("probe", []) if p.get("point")])
            if asyncio.iscoroutine(r):
                if sess.loop is not None:
                    r = asyncio.run_coroutine_threadsafe(r, sess.loop).result(10)
                else:
                    r = asyncio.run(asyncio.wait_for(r, 10))
        except Exception as e:
            return {"outcome": "rejected", "detail": str(e), "reply": f"Highlight failed: {e}"}
        r = r or {}
        done, missing = r.get("highlighted") or [], r.get("missing") or []
        if r.get("error") or r.get("message") and not done:
            msg = r.get("error") or r.get("message")
            return {"outcome": "rejected", "detail": msg, "reply": msg}
        reply = ("Highlighted " + ", ".join(done) + f" in {getattr(sess, 'ecad_program', 'Fusion')}") if done else "Nothing to highlight for " + tid
        if missing:
            reply += f". Not found: {', '.join(missing)}"
        return {"outcome": "applied", "detail": reply, "reply": reply + "."}

    def _do_repeat(self, res, n, text):
        return {"outcome": "applied", "detail": "repeat", "reply": self.last_reply or "Nothing to repeat."}

    # ---- AOI issue review by voice (Visual step, review tab). Same calls the buttons make: engine.aoi_decide + the review cursor.
    def _aoi_on(self) -> bool:
        step = self._step()
        return bool(self.ctx.get("aoi_review") and step and step.get("kind") == "visual" and self.eng.state.aoi_current_run())

    def _aoi_items(self) -> list[dict]:
        """The review list in the order the review page shows it: ranked candidates, then operator-found issues."""
        import json
        from ..aoi import review_merge as M
        run = self.eng.state.aoi_current_run()
        if not run:
            return []
        items = M.review_items(json.loads(Path(run["result"]).read_text()))
        items += [dict(m) for m in self.eng.state.aoi_manual.values()]
        return items

    def _aoi_say(self, it: dict) -> str:
        from ..aoi import link
        des = it.get("designators") if it.get("manual") else link.designators_of(it["id"])
        kind = (it.get("manual_kind") if it.get("manual") else it.get("kind")) or "issue"
        return f"{' '.join(des or [])} {str(kind).lower()} on {spoken_board(it['board'])}".strip()

    def _aoi_cursor(self, items: list[dict]) -> int:
        from ..aoi import review
        return min(max(review.engine_cursor(self.eng), 0), max(len(items) - 1, 0))

    def _aoi_goto(self, items, i: int):
        from ..aoi import review
        review.set_cursor(i)
        self._bc({"kind": "voice_aoi", "key": items[i]["key"]})

    def _aoi_decision(self, key):
        d = self.eng.state.aoi_decision_for(key)
        return d["decision"] if d else None

    def _aoi_next_undecided(self, items, frm: int):
        n = len(items)
        return next(((frm + k) % n for k in range(1, n + 1) if not self._aoi_decision(items[(frm + k) % n]["key"])), None)

    def _aoi_guard(self):
        p = self.pending
        if p and p.get("aoi"):                  # the operator carried on without cancelling: the earlier decision counts
            self._drop_pending()
            try:
                self._aoi_apply(p["aoi"])
            except CmdError:
                pass
        items = self._aoi_items()
        if not self._aoi_on() or not items:
            return None, {"outcome": "rejected", "detail": "no AOI review", "reply": "Open the issue review in the Visual step first."}
        return items, None

    def _aoi_decide_cmd(self, decision: str, words: str):
        items, err = self._aoi_guard()
        if err:
            return err
        i = self._aoi_cursor(items)
        it = items[i]
        nxt = self._aoi_next_undecided(items, i)
        tail = f" Next: {self._aoi_say(items[nxt])}." if nxt is not None and nxt != i else " No undecided issues left." if nxt is None else ""
        grace = self.cfg["grace_s"]
        out = {"outcome": "applied", "detail": f"{it['key']} {decision}", "boards": [it["board"]],
               "reply": f"{self._aoi_say(it)}: {words}.{tail}"}
        if grace:
            out["outcome"] = "proposed"
            out["detail"] = f"{it['key']} {decision}, applies in {grace:g} s unless cancelled"
            out["aoi_grace"] = {"key": it["key"], "decision": decision, "next": items[nxt]["key"] if nxt is not None else None}
            return out
        self._aoi_apply({"key": it["key"], "decision": decision, "next": items[nxt]["key"] if nxt is not None else None})
        return out

    def _aoi_apply(self, a: dict):
        self.eng.aoi_decide(a["key"], a["decision"], "")
        items = self._aoi_items()
        j = next((k for k, x in enumerate(items) if x["key"] == a["next"]), None) if a.get("next") else None
        if j is not None:
            self._aoi_goto(items, j)
        self._bc({"kind": "voice_aoi", "key": (items[j]["key"] if j is not None else a["key"]), "sync": True})
        self._bc({"kind": "state"})

    def _do_aoi_confirm(self, res, n, text):
        return self._aoi_decide_cmd("fixed", "defect fixed")

    def _do_aoi_reject(self, res, n, text):
        return self._aoi_decide_cmd("rejected", "not an issue")

    def _do_aoi_unsure(self, res, n, text):
        return self._aoi_decide_cmd("unsure", "ignored")

    def _aoi_move(self, step: int, undecided: bool = False):
        items, err = self._aoi_guard()
        if err:
            return err
        i = self._aoi_cursor(items)
        if undecided:
            j = self._aoi_next_undecided(items, i)
            if j is None:
                return {"outcome": "rejected", "detail": "none undecided", "reply": "No undecided issues left."}
        else:
            j = (i + step) % len(items)
        self._aoi_goto(items, j)
        d = self._aoi_decision(items[j]["key"])
        return {"outcome": "applied", "detail": items[j]["key"], "nav": False,
                "reply": f"{self._aoi_say(items[j])}, {j + 1} of {len(items)}{', ' + {'confirmed': 'defect', 'rejected': 'no fault found', 'unsure': 'ignored'}[d] if d else ', undecided'}."}

    def _do_aoi_next(self, res, n, text):
        return self._aoi_move(1)

    def _do_aoi_prev(self, res, n, text):
        return self._aoi_move(-1)

    def _do_aoi_next_undecided(self, res, n, text):
        return self._aoi_move(1, undecided=True)

    def _do_aoi_read(self, res, n, text):
        items, err = self._aoi_guard()
        if err:
            return err
        it = items[self._aoi_cursor(items)]
        reason = ((f"{it['value']}. " if it.get("value") else "") + (it.get("note") or "")).strip()
        return {"outcome": "applied", "detail": it["key"], "reply": f"{self._aoi_say(it)}. {reason or 'No reason given.'}"}

    # ---- feeds, switches, status -------------------------------------------------------------------------------------------
    def feed_wav(self, path: str, via: str = "ptt") -> dict:
        return self.capture.feed_wav(path, via)

    def enable(self, on: bool) -> dict:
        self.capture.set_enabled(bool(on))
        return self.status()

    def handsfree(self, on: bool) -> dict:
        self.capture.set_handsfree(bool(on))
        return self.status()

    def set_visible(self, visible: bool, client: str | None = None) -> dict:
        self.capture.set_visible(bool(visible), client)
        return self.status()

    def meter_blocked(self) -> bool:
        """R11: while push-to-talk is held or an utterance is being heard or handled, the live meter proposes and auto-accepts nothing."""
        c = self.capture
        return bool(c.recording or c.busy or c.seg.active)

    def ptt_start(self) -> dict:
        self.tts.stop()                      # the operator speaks over the read-back
        self.capture.ptt_start(beats=True)
        return self.status()

    def ptt_beat(self) -> dict:
        self.capture.ptt_beat()
        return self.status()

    def ptt_stop(self) -> dict:
        self.capture.ptt_stop()
        return self.status()

    def ptt_cancel(self) -> dict:
        self.capture.ptt_cancel()
        return self.status()

    def set_config(self, patch: dict) -> dict:
        new = vcfg.validate(patch)
        with self._lock:
            self.cfg.update(new)
            vcfg.save(self.cfg)
        self.capture.apply_config()
        self._llm_cache = None
        self._changed()
        return self.status()

    def models(self, fresh: bool = False) -> dict:
        """Installed Ollama models (loopback only), with the parameter size Ollama reports; embedding models left out."""
        now = time.monotonic()
        if not fresh and self._llm_cache and now - self._llm_cache[0] < 10:
            return self._llm_cache[1]
        try:
            url = vcfg.loopback_only(self.cfg["ollama_url"])
        except ValueError as e:
            out = {"ok": False, "models": [], "details": [], "default": vcfg.DEFAULT_LLM, "detail": str(e)}
            self._llm_cache = (now, out)
            return out
        try:
            with urllib.request.urlopen(url + "/api/tags", timeout=0.8) as r:
                raw = json.load(r).get("models", [])
            seen, details = set(), []
            for m in raw:
                name = m.get("name", "")
                fam = " ".join((m.get("details") or {}).get("families") or [])
                if not name or name in seen or "embed" in name or "bert" in fam:
                    continue
                seen.add(name)
                details.append({"name": name, "size": (m.get("details") or {}).get("parameter_size") or "", "bytes": m.get("size") or 0})
            out = {"ok": True, "models": [d["name"] for d in details], "details": details, "default": vcfg.DEFAULT_LLM, "detail": ""}
        except Exception as e:
            out = {"ok": False, "models": [], "details": [], "default": vcfg.DEFAULT_LLM,
                   "detail": f"Ollama not reachable at {url} ({e.__class__.__name__})"}
        self._llm_cache = (now, out)
        return out

    @staticmethod
    def _installed(name: str, names: list[str]) -> str | None:
        for x in names:
            if x == name or x == name + ":latest":
                return x
        return None

    @staticmethod
    def _best(details: list[dict]) -> str | None:
        """Fallback while the chosen model is not pulled: the PoC's gemma4:26b if present, else the largest installed model."""
        names = [d["name"] for d in details]
        if "gemma4:26b" in names:
            return "gemma4:26b"
        return max(details, key=lambda d: d.get("bytes") or 0)["name"] if details else None

    def voices(self) -> dict:
        v = self.tts.voices()
        return {"voices": v, "natural": [n for n in v if is_natural(n)]}

    def test_voice(self, voice: str | None = None) -> dict:
        return self.tts.test(voice)

    def _llm_status(self) -> dict:
        """The model that will run: the configured one if installed, else (until it is pulled) the best installed model."""
        c = self.cfg
        want = c["llm_model"]
        if not c["llm_enabled"]:
            return {"ok": False, "model": want, "wanted": want, "fallback": False, "models": [], "detail": "LLM off: grammar only", "install": ""}
        m = self.models()
        if not m["ok"]:
            return {"ok": False, "model": want, "wanted": want, "fallback": False, "models": [], "details": [], "detail": m["detail"],
                    "install": f"ollama serve, then: ollama pull {want}"}
        have = self._installed(want, m["models"])
        if have:
            return {"ok": True, "model": have, "wanted": want, "fallback": False, "models": m["models"], "details": m["details"],
                    "detail": "", "install": ""}
        best = self._best(m["details"])
        what = "Default model" if want == vcfg.DEFAULT_LLM else "Model"
        return {"ok": bool(best), "model": best or want, "wanted": want, "fallback": bool(best), "models": m["models"], "details": m["details"],
                "detail": f"{what} {want} not installed: run `ollama pull {want}`" + (f"; using {best} until then" if best else ""),
                "install": f"ollama pull {want}"}

    def privacy_line(self, stt_s: dict, llm_s: dict) -> str:
        stt_txt = "not available" if not stt_s.get("ok") else ("sidecar text (test mode)" if stt_s.get("model") == "sidecar" else f"{stt_s['model']} (cached)")
        if not self.cfg["llm_enabled"]:
            llm_txt = "off (grammar only)"
        else:
            try:
                host = (urlsplit(vcfg.loopback_only(self.cfg["ollama_url"])).hostname or "")
            except ValueError:
                host = "refused (not local)"
            llm_txt = f"{llm_s['model']} at {host}"
        return f"Voice: local only: STT {stt_txt}, LLM {llm_txt}"

    def status(self) -> dict:
        cap = self.capture
        stt_s = cap.stt_status()
        llm_s = self._llm_status()
        mic = cap.mic_status()
        if cap.mic_error:
            mic = {"ok": False, "detail": cap.mic_error, "install": INSTALL}
        with self._lock:
            p = self.pending
            pend = None
            if p:
                left = max(0.0, self.cfg["grace_s"] - (time.monotonic() - p["t0"])) if p["armed"] else self.cfg["grace_s"]
                pend = {"utt": p["utt"], "deadline_s": round(left, 2), "armed": p["armed"]}
            ctx = {**self.ctx, "visible": cap.visible}
        return {"enabled": cap.enabled, "handsfree": cap.handsfree, "listening": cap.rec is not None, "recording": cap.recording,
                "speaking": self.tts.speaking(), "muted_tts": self.tts.muted or not self.cfg["tts_enabled"], "level": cap.level(),
                **cap.stream_info(),
                "in_speech": cap.seg.active, "busy": cap.busy, "stt": stt_s, "mic": mic, "llm": llm_s, "tts": self.tts.status(),
                "privacy": self.privacy_line(stt_s, llm_s),
                "nlu": {"ok": understand is not None, "detail": "" if understand else "voice/understand.py is not installed"},
                "config": dict(self.cfg), "pending": pend, "context": ctx, "bench": self.bench_status(), "guide": self.guide_status()}
