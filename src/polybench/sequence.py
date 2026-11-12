"""Automated step: one Run for the whole board (docs/AUTOMATED_STEP.md).

`SequenceRunner` drives the ordinary ActionRunner one action at a time, so every plug-in, child process, abort, safe state and journal
event is the existing mechanism. It adds one journal event type, `sequence` (phases start, end, proceed, defects, final), the operator
question (`operator_check`) and the final image choice. It never computes a verdict: the engine does, as for any proposal.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

from .engine import CmdError
from . import benchcheck, doctor
from .protocol import SEQ_SECTION, seq_items

IMAGE_EXTS = (".hex", ".bin", ".elf")
HEX64 = re.compile(r"\b[0-9a-fA-F]{64}\b")
YES = {"yes", "y", "true", "1", "ok", "confirm", "confirmed"}
# UIR-01-R2 (TBR, closes at the bench with the maintainer): a passed equipment check is reused for 4 hours. Long enough for a batch session at one
# bench, short enough that a cable or adapter swapped over lunch is looked at again. Beyond the clock it is dropped at once when the USB
# device list changes, when an action reports an error, or when the step's equipment list changes.
BENCH_REUSE_S = 4 * 3600
# A "check again" that does not come from the on-screen button (the voice path) is ignored inside this window: the spoken prompt must not
# be able to trigger itself (journal 2250 to 2260: five asks in 72 s).
VOICE_RECHECK_MIN_S = 15.0


def usb_fingerprint(simulate: bool):
    """A hashable picture of the USB device list, or None when it cannot be read (simulation, no ioreg). Blocking: run in a thread."""
    if simulate:
        return None
    devs = doctor.usb_devices()
    if devs is None:
        return None
    return tuple(sorted((str(d.get("vid")), str(d.get("pid")), str(d.get("name", ""))) for d in devs))


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest_labels(path) -> dict[str, str]:
    """{sha256: label} from a manifest file: any line holding a 64-hex hash (a sha256sum list or a markdown table). The label is the first
    backticked word of the line, else the file name next to the hash."""
    out: dict[str, str] = {}
    try:
        text = Path(str(path)).expanduser().read_text(errors="replace", encoding="utf-8")
    except OSError:
        return out
    for ln in text.splitlines():
        hs = HEX64.findall(ln)
        if not hs:
            continue
        m = re.search(r"`([^`]{1,64})`", ln)
        if m and not HEX64.fullmatch(m.group(1)):
            lab = m.group(1)
        else:
            rest = HEX64.sub("", ln).replace("|", " ").replace("*", " ").replace("`", " ").split()
            lab = Path(rest[0]).name if rest else "manifest entry"
        for h in hs:
            out.setdefault(h.lower(), lab)
    return out


def actions_of(step: dict) -> dict:
    sec = next((x for x in step.get("sections", []) if x["id"] == SEQ_SECTION), {})
    return {a["id"]: a for a in sec.get("actions", []) if isinstance(a, dict) and "id" in a}


def final_action(step: dict):
    acts = actions_of(step)
    for it in seq_items(step):
        if it["kind"] == "action" and acts.get(it["id"], {}).get("final"):
            return acts[it["id"]]
    return None


def plural(n, w):
    return f"{n} {w}{'' if n == 1 else 's'}"


def verdict_summary(rows: list[dict], image_label: str = "", has_final: bool = False) -> dict:
    """The spoken and shown end summary: 'N passed, M failed: names.' then the final-image offer, or the defects offer."""
    skipped = [r for r in rows if r["src"] == "skipped"]
    scored = [r for r in rows if not r.get("final") and r["src"] != "skipped"]
    bad = [r for r in scored if r["src"] == "none" or not r["clean"]]
    good = len(scored) - len(bad)
    names = ", ".join(r["title"] for r in bad)
    text = f"{good} passed, {len(bad)} failed" + (f": {names}." if bad else ".") + (f" {len(skipped)} skipped." if skipped else "")
    if bad:
        nxt = "Record defects, or diagnose."
    elif has_final:
        nxt = f"Ready to program the final image, {image_label or 'the default image'}. Say confirm."
    else:
        nxt = "Ready to accept. Say confirm."
    return {"passed": good, "failed": len(bad), "skipped": len(skipped), "failed_names": [r["title"] for r in bad], "text": text, "next": nxt,
            "spoken": f"{text} {nxt}", "ok": not bad}


class SequenceRunner:
    def __init__(self, engine, runner, broadcast, simulate: bool):
        self.engine, self.runner, self.broadcast, self.simulate = engine, runner, broadcast, simulate
        runner.seq = self
        self.active = False
        self.title = ""
        self.board = ""
        self.live: dict | None = None
        self._abort = False
        self._ask: asyncio.Future | None = None
        self._task: asyncio.Task | None = None
        self.listeners: list = []                 # called with (kind, info) at 'end', 'final' and 'ask' (voice)
        self._bench_stash: dict[str, dict] = {}   # the check a failed final flash dropped; a later successful flash proves the probe and restores it
        self._bench_cache: dict[str, dict] = {}   # step id -> {results, ts, fp, sig}: the last passed equipment check (UIR-01-R2)
        self.bench_reuse_s = BENCH_REUSE_S
        self.usb_fingerprint = lambda: usb_fingerprint(simulate)
        self.usb_poll_s = 2.0
        self.usb_recheck_min_s = 10.0                 # TBR: an unplug/replug loop on a wanted device re-asks at most this often
        self._usb_recheck_at = -1e9
        self.voice_recheck_min_s = VOICE_RECHECK_MIN_S
        self._checked_at = 0.0
        self._fresh_bench = False
        self._ticked: set = set()
        self._seq = sum(1 for e in engine.journal.events if e["type"] == "sequence" and e["payload"].get("phase") == "start")

    # ---- status
    def status(self):
        return None if self.live is None else {**self.live, "abort": self._abort}

    def gap_status(self):
        lv = self.live or {}
        cur = next((i for i in lv.get("items", []) if i["state"] in ("running", "asking")), None)
        return {"run": 0, "plugin": "sequence", "board": self.board, "step": lv.get("step"), "section": SEQ_SECTION,
                "action": (cur or {}).get("id", ""), "title": self.title, "fraction": 0.0, "text": "sequence", "ask": None,
                "state": "running", "proposes": []}

    def request_abort(self):
        self._abort = True
        if self._ask is not None and not self._ask.done():
            self._ask.cancel()

    async def _push(self):
        await self.broadcast({"kind": "sequence", "live": self.status()})
        await self.broadcast({"kind": "state"})

    def _notify(self, kind, info):
        for fn in list(self.listeners):
            try:
                fn(kind, info)
            except Exception:
                pass

    def verdicts(self) -> dict:
        """For the page: the verdict rows and the end summary of every finished run whose verdict is not recorded yet."""
        out = {}
        for (b, sid), rec in self.engine.state.sequences.items():
            if rec.get("outcome") in (None, "running") or rec.get("proceed") or rec.get("defects"):
                continue
            st = self.engine.proto.step(sid)
            if not st:
                continue
            rows = self.engine.seq_rows(b, sid)
            im = self.default_image(st)
            fa = final_action(st)
            sk = (rec.get("bench") or {}).get("skips") or {}
            has_final = bool(fa) and not [n for n in fa.get("needs") or [] if n in sk]
            out[f"{b}|{sid}"] = {"rows": rows, "summary": verdict_summary(rows, (im or {}).get("label", ""), has_final), "has_final": has_final}
        return out

    # ---- start
    def _step(self, step_id):
        s = self.engine.proto.step(step_id)
        if not s or s.get("kind") != "automated":
            raise CmdError("not an automated step", 404)
        return s

    async def start(self, step_id, board, confirmed=False, fresh_bench=False):
        s = self._step(step_id)
        eng = self.engine
        eng._board(board)
        eng._check_open(board, s)
        if self.active or self.runner.current:
            raise CmdError("something is already running; wait for it or abort it", 409)
        held = [r["test"] for r in eng.seq_rows(board, step_id) if r["src"] == "accepted"]
        if held:
            # 19 Oct (maintainer): a run of an automated step is a new attempt; earlier results stay in the previous attempt.
            eng.retest(board, step_id, cause=f"automated re-run; earlier results kept ({', '.join(held[:5])})", auto=True, plugin="sequence")
        acts = actions_of(s)
        items = seq_items(s)
        fin = final_action(s)
        prompts = [str((acts[i["id"]].get("params") or {}).get("prompt") or "").strip() for i in items
                   if i["kind"] == "action" and acts[i["id"]] is not fin]
        prompts = [p for p in prompts if p]
        if prompts and not confirmed:
            return {"ok": True, "needs_prompt": prompts, "title": s["title"], "board": board}
        if prompts:
            eng._emit("action_confirm", {"board": board, "action": "sequence", "step": step_id, "prompt": " / ".join(prompts)})
        self._seq += 1
        plan = []
        for it in items:
            a = acts.get(it["id"], {})
            plan.append({"id": it["id"], "kind": it["kind"], "title": a.get("title") or it.get("title") or it["id"],
                         "final": bool(a.get("final")), "test": it.get("test"), "question": it.get("question"),
                         "show": a.get("show") or [], "proposes": a.get("proposes") or ([it["test"]] if it.get("test") else [])})
        if s.get("equipment"):
            plan.insert(0, {"id": "_bench", "kind": "bench", "title": "Equipment and connections", "final": False, "test": None, "question": None, "show": [],
                            "proposes": []})
        attempt = eng.state.attempt(board, step_id)
        eng.seq_event(board, step_id, "start", seq=self._seq, attempt=attempt, items=plan, simulate=self.simulate)
        self.active, self.title, self.board, self._abort, self._fresh_bench = True, s["title"], board, False, bool(fresh_bench)
        self.live = {"seq": self._seq, "step": step_id, "board": board, "phase": "run", "t0": time.time(), "attempt": attempt,
                     "items": [{**p, "state": "pending", "dur_s": None, "run": None, "t0": None, "answer": None} for p in plan], "ask": None,
                     "bench": None, "skips": {}}
        self._task = asyncio.create_task(self._run(s, board, acts, fin))
        await self._push()
        return {"ok": True, "seq": self._seq}

    # ---- the run
    def _stop_hit(self, board, step, tests, run):
        """A hard FAIL proposal on a test marked stop: true, from this run: the test id, else None."""
        for tid in tests:
            t = self.engine.state.test_index.get(tid, (0, 0, {}))[2]
            if t.get("stop") is not True:
                continue
            c = self.engine.state.cell(board, tid)
            props = [q for q in c["proposals"].values() if (q.get("source") or {}).get("run_ref") == run or (q.get("source") or {}).get("key", "").startswith("operator_check")]
            if c["status"] == "accepted":
                props = [c]
            if any(q.get("verdict") == "fail" for q in props):
                return tid
        return None

    async def _run(self, s, board, acts, fin):
        step_id = s["id"]
        items = self.live["items"]
        stop = None
        outcome = "complete"
        try:
            for it in items:
                if it["final"]:
                    continue
                if self._abort:
                    outcome, stop = "aborted", stop or {"item": it["id"], "reason": "aborted by the operator"}
                    break
                it["t0"] = time.time()
                hit = self._needs_skipped(s, it)
                if hit:
                    it.update(state="skipped", dur_s=0.0, text="skipped, needs " + ", ".join(hit))
                    await self._push()
                    continue
                it["state"] = "asking" if it["kind"] == "check" else "running"
                await self._push()
                if it["kind"] == "action":
                    state, text, run = await self._do_action(step_id, board, it)
                elif it["kind"] == "bench":
                    state, text, run = await self._do_bench(s, board, it)
                else:
                    state, text, run = await self._do_check(step_id, board, it)
                it.update(state=state, run=run, dur_s=round(time.time() - it["t0"], 1), text=text)
                await self._push()
                if state == "cancelled" or (self._abort and state != "ok"):
                    it["state"] = "cancelled"
                    outcome, stop = "aborted", {"item": it["id"], "reason": "aborted by the operator"}
                    break
                if state == "error":
                    self._bench_stash.pop(step_id, None)
                    self._bench_cache.pop(step_id, None)        # an action that cannot run may mean missing equipment: look again next time
                    outcome, stop = "error", {"item": it["id"], "reason": text or "the action failed"}
                    break
                hit = self._stop_hit(board, step_id, it["proposes"], run)
                if hit:
                    tt = self.engine.state.test_index[hit][2]
                    outcome, stop = "stopped", {"item": it["id"], "test": hit, "reason": f"{hit} {tt.get('title', '')}: FAIL on a stop test"}
                    break
        except asyncio.CancelledError:
            outcome, stop = "aborted", {"item": "", "reason": "aborted"}
        except Exception as e:          # noqa: BLE001  (never leave the bench marked busy)
            outcome, stop = "error", {"item": "", "reason": f"sequence error: {e}"}
        for it in items:
            if it["state"] == "pending" and not it["final"]:
                it["state"] = "not_run"
        total = round(time.time() - self.live["t0"], 1)
        self.engine.seq_event(board, step_id, "end", seq=self.live["seq"], attempt=self.live["attempt"], outcome=outcome, stop=stop, total_s=total,
                              items=[{k: i.get(k) for k in ("id", "kind", "state", "dur_s", "run", "text", "answer")} for i in items])
        self.active = False
        self.live = None
        await self._push()
        rows = self.engine.seq_rows(board, step_id)
        lab = (self.default_image(s) or {}).get("label", "")
        self._notify("end", {"board": board, "step": step_id, "outcome": outcome, "stop": stop,
                             "summary": verdict_summary(rows, lab, fin is not None) if outcome == "complete" else None})

    def _title_of(self, s, eid):
        e = next((x for x in s.get("equipment") or [] if isinstance(x, dict) and x.get("id") == eid), {})
        return e.get("title") or eid

    def _needs_skipped(self, s, it, skips=None):
        """Titles of the skipped equipment this sequence entry needs (an action's or an operator check's `needs`)."""
        sk = skips if skips is not None else (self.live or {}).get("skips") or {}
        needs = it.get("needs")
        if needs is None:
            a = actions_of(s).get(it["id"]) or {}
            needs = a.get("needs") or []
        return [self._title_of(s, n) for n in needs if n in sk]

    def _apply_skips(self, s, board, skips):
        """Every result test that needs skipped equipment is recorded as skipped, with the operator's reason."""
        for sec in s.get("sections", []):
            if sec.get("diagnose") or sec.get("hidden"):
                continue
            for t in sec.get("tests", []):
                for n in t.get("needs") or []:
                    if n in skips:
                        try:
                            self.engine.skip(board, t["id"], f"equipment {self._title_of(s, n)} skipped: {skips[n]}")
                        except CmdError:
                            pass
                        break

    async def _do_action(self, step_id, board, it):
        try:
            r = await self.runner.start(step_id, SEQ_SECTION, it["id"], board, True, from_sequence=True)
        except CmdError as e:
            return "error", str(e), None
        if not r.get("ok"):
            return "error", "could not start the action", r.get("run")
        res = await self.runner.wait(r["run"])
        return res["state"], res.get("text", ""), r["run"]

    # ---- the bench check (UIR-01-R2, R3)
    @staticmethod
    def _sig(eq) -> str:
        return json.dumps(eq, sort_keys=True, default=str)

    def bench_cache_status(self) -> dict:
        """For the page: the passed equipment checks that are still inside the reuse window, by step id."""
        now = time.time()
        return {sid: {"results": c["results"], "age_s": round(now - c["ts"]), "window_s": self.bench_reuse_s, "for_board": c.get("for_board")}
                for sid, c in self._bench_cache.items() if now - c["ts"] <= self.bench_reuse_s}

    def _remember_bench(self, step_id, eq, results, fp, ticked=(), for_board=None):
        if benchcheck.blocking(results, {}, set(ticked or [])):
            self._bench_cache.pop(step_id, None)
            return False
        self._bench_cache[step_id] = {"results": results, "ts": time.time(), "fp": fp, "sig": self._sig(eq), "ticked": sorted(ticked or []), "for_board": for_board}
        return True

    async def _reusable_bench(self, step_id, eq):
        c = self._bench_cache.get(step_id)
        if not c:
            return None
        if time.time() - c["ts"] > self.bench_reuse_s or c["sig"] != self._sig(eq):
            self._bench_cache.pop(step_id, None)
            return None
        if c["fp"] is not None:
            fp = await asyncio.to_thread(self.usb_fingerprint)
            if fp is not None and fp != c["fp"]:
                self._bench_cache.pop(step_id, None)     # a device was plugged or unplugged since the check
                return None
        return c

    @staticmethod
    def _wanted_vids(items) -> set[str]:
        """USB vendor ids (as the fingerprint spells them) of the missing items the check is waiting for. Items with no USB id are not watched."""
        out: set[str] = set()
        for e in items:
            raw = e.get("vid_pid")
            if not raw and isinstance(e.get("match"), dict):
                raw = e["match"].get("vid_pid")
            vp = benchcheck.parse_vid_pid(raw)
            if e.get("kind") == "pcan":
                out.add(str(doctor.PEAK_VID))
            elif vp:
                out.add(str(vp[0]))
            elif e.get("kind") == "openocd_probe":
                try:
                    from .plugins.openocd import PROBES
                    out.update(str(v) for v in (PROBES.get(e.get("probe", "atmel-ice"), {}).get("vids") or ()))
                except Exception:       # noqa: BLE001
                    pass
        return out

    async def _watch_usb(self, fp0, fut, wanted):
        """While the bench question is open: a change of the USB list that concerns a device the check is waiting for re-runs the check,
        at most once per usb_recheck_min_s. Any other change, or no wanted device, waits for the operator's Check again."""
        if fp0 is None or not wanted:
            return
        while not fut.done():
            await asyncio.sleep(self.usb_poll_s)
            try:
                fp = await asyncio.to_thread(self.usb_fingerprint)
            except Exception:       # noqa: BLE001
                continue
            if fp is None or fut.done():
                continue
            diff = set(fp) ^ set(fp0)
            if any(d[0] in wanted for d in diff) and time.monotonic() - self._usb_recheck_at >= self.usb_recheck_min_s:
                self._usb_recheck_at = time.monotonic()
                fut.set_result(("recheck", {}, "usb"))
                return

    async def _do_bench(self, s, board, it):
        eq = [e for e in s["equipment"] if isinstance(e, dict)]
        step_id = s["id"]
        loop = asyncio.get_running_loop()
        seq, att = self.live["seq"], self.live["attempt"]
        c = None if self._fresh_bench else await self._reusable_bench(step_id, eq)
        if c is not None:
            age = round(time.time() - c["ts"])
            results = c["results"]
            reused = c.get("for_board") != board            # a check the operator pressed for this very board is not "reused"
            self.engine.seq_event(board, step_id, "bench", seq=seq, attempt=att, reused=reused, age_s=age,
                                  equipment=[{k: v for k, v in e.items() if k != "sim"} for e in eq], results=results)
            self.engine.seq_event(board, step_id, "bench_ok", seq=seq, attempt=att, skips={}, reused=reused)
            self.live["bench"] = {"results": results, "checking": False, "skips": {}, "reused": reused, "age_s": age}
            self.live["skips"] = {}
            n_ok = sum(1 for r in results if r["state"] != "missing")
            mins = max(1, round(age / 60))
            return "ok", f"{n_ok} of {len(results)} ready, {'reused: ' if reused else ''}checked {mins} min ago", None
        trigger = "operator"
        while True:
            self.live["bench"] = {"results": None, "checking": True, "skips": {}}
            await self._push()
            fp0 = await asyncio.to_thread(self.usb_fingerprint)
            results = await asyncio.to_thread(benchcheck.run_checks, eq, self.simulate)
            self._checked_at = time.monotonic()
            self.engine.seq_event(board, step_id, "bench", seq=seq, attempt=att,
                                  equipment=[{k: v for k, v in e.items() if k != "sim"} for e in eq], results=results, trigger=trigger)
            self.live["bench"] = {"results": results, "checking": False, "skips": {}}
            missing = [r for r in results if r["required"] and r["state"] == "missing"]
            self._ticked = set()
            self._ask = loop.create_future()
            self.live["ask"] = {"kind": "bench", "id": it["id"], "question": "Bench ready, start the protocol", "missing": [r["id"] for r in missing]}
            it["state"] = "asking"
            await self._push()
            # The prompt must not contain the words the voice path listens for ("check again", "recheck"): a speaker the microphone hears
            # could otherwise answer its own question.
            spoken = ("Bench check: " + "; ".join(f"{r['name']} missing" for r in missing) + ". Fix it, then use the button on screen, or override."
                      if missing else "Bench ready. Say confirm to start the protocol.")
            self._notify("ask", {"board": board, "question": spoken, "kind": "bench"})     # asked once per check, never on a timer
            watcher = asyncio.create_task(self._watch_usb(fp0, self._ask, self._wanted_vids([e for e in eq if e["id"] in {r["id"] for r in missing}])))
            try:
                reply = await self._ask
            except asyncio.CancelledError:
                self.live["ask"] = None
                return "cancelled", "", None
            finally:
                watcher.cancel()
                self._ask = None
            action, overrides = reply[0], reply[1]
            trigger = reply[2] if len(reply) > 2 else "operator"
            self.live["ask"] = None
            if action == "recheck":
                it["state"] = "running"
                continue
            ov = {k: str(v).strip() for k, v in (overrides or {}).items() if str(v).strip() and any(r["id"] == k for r in results)}
            self.engine.seq_event(board, step_id, "bench_ok", seq=seq, attempt=att, skips=ov)
            self.live["bench"]["skips"] = ov
            self.live["skips"] = ov
            self._apply_skips(s, board, ov)
            if not ov:
                self._remember_bench(step_id, eq, results, fp0, self._ticked)
            n_ok = sum(1 for r in results if r["state"] != "missing" and r["id"] not in ov)
            return "ok", f"{n_ok} of {len(results)} ready" + (f", {len(ov)} skipped" if ov else ""), None

    async def bench_check_only(self, step_id, ticked=(), board=None):
        """The 'Check again' control before a run: look at the equipment (read only). A pass is kept for reuse by the next run; a miss
        drops the kept one. Nothing is journalled."""
        s = self._step(step_id)
        if self.active:
            raise CmdError("a sequence is running", 409)
        eq = [e for e in s.get("equipment") or [] if isinstance(e, dict)]
        fp = await asyncio.to_thread(self.usb_fingerprint)
        results = await asyncio.to_thread(benchcheck.run_checks, eq, self.simulate)
        self._checked_at = time.monotonic()
        kept = self._remember_bench(step_id, eq, results, fp, ticked, board)
        return {"ok": True, "results": results, "kept": kept}

    async def bench_reply(self, action, overrides=None, ticked=(), via=None):
        """The operator's reply to the bench check: 'confirm' (every required item passes, or has a typed reason) or 'recheck'.
        `via` is "ui" for the on-screen buttons. Any other source (the voice path) cannot re-check within voice_recheck_min_s of a check."""
        if self._ask is None or self._ask.done() or not (self.live or {}).get("ask") or self.live["ask"].get("kind") != "bench":
            raise CmdError("no bench check is waiting", 409)
        if action == "recheck" and via != "ui" and time.monotonic() - self._checked_at < self.voice_recheck_min_s:
            raise CmdError("the equipment was checked a moment ago: use the Check again button on screen", 409)
        if action == "confirm":
            res = (self.live.get("bench") or {}).get("results") or []
            blk = benchcheck.blocking(res, overrides or {}, set(ticked or []))
            if blk:
                raise CmdError("required equipment is not ready: " + "; ".join(f"{r['name']} ({r['detail']})" for r in blk) +
                               ". Fix it and check again, or type a reason to override.", 409, missing=[r["id"] for r in blk])
        elif action != "recheck":
            raise CmdError("action must be confirm or recheck", 422)
        self._ticked = set(ticked or [])
        self._ask.set_result((action, overrides or {}, "operator"))
        return {"ok": True}

    async def _do_check(self, step_id, board, it):
        loop = asyncio.get_running_loop()
        self._ask = loop.create_future()
        self.live["ask"] = {"kind": "check", "id": it["id"], "question": it["question"], "test": it["test"], "title": it["title"]}
        await self._push()
        self._notify("ask", {"board": board, "question": it["question"]})
        try:
            val, note, via = await self._ask
        except asyncio.CancelledError:
            self.live["ask"] = None
            return "cancelled", "", None
        finally:
            self._ask = None
        self.live["ask"] = None
        yes = str(val).strip().lower() in YES
        src = {"kind": "operator_check", "component": "operator", "key": "operator_check:" + it["id"], "operator": self.engine.operator,
               "input_method": via}
        self.engine.add_proposal(board, it["test"], "yes" if yes else "no", None, None, f"operator answered {'yes' if yes else 'no'}: {it['question']}",
                                 source=src, via=via, question=it["question"])
        if note and str(note).strip():
            self.engine.note(str(note).strip(), board, it["test"], step=step_id)
        it["answer"] = "yes" if yes else "no"
        return "ok", f"you answered {it['answer']}", None

    async def answer(self, value, note="", via="keyboard"):
        """The operator's answer to the open question. Never an assistant: the route is operator-only."""
        if self._ask is None or self._ask.done() or (self.live or {}).get("ask", {}).get("kind") != "check":
            raise CmdError("no question is waiting", 409)
        self._ask.set_result((value, note, via))
        return {"ok": True}

    # ---- final image
    def default_image(self, s):
        imgs = s.get("final_images") or []
        return next((i for i in imgs if i.get("default")), imgs[0] if imgs else None)

    def image_info(self, s, choice: dict | None) -> dict:
        """Resolve the operator's choice to {label, path, sha256, source, manifest, in_manifest, reason}. Raises CmdError for anything the
        flash must not start with: missing file, hash that differs from the protocol, an unlisted file without a typed reason."""
        choice = choice or {}
        imgs = s.get("final_images") or []
        known = manifest_labels(s["manifest"]) if s.get("manifest") else {}
        for im in imgs:
            if im.get("sha256"):
                known.setdefault(str(im["sha256"]).lower(), im.get("label", ""))
        if choice.get("source") == "file":
            pth = Path(str(choice.get("path") or "")).expanduser()
            if pth.suffix.lower() not in IMAGE_EXTS:
                raise CmdError("choose a .hex, .bin or .elf file", 422)
            if not pth.is_file():
                raise CmdError(f"image file not found: {pth}", 404)
            digest = sha256_of(pth)
            lab = known.get(digest)
            reason = str(choice.get("reason") or "").strip()
            if not lab and not reason:
                raise CmdError("this file is not in the manifest: type the reason for using it", 422, needs_reason=True, sha256=digest)
            return {"label": pth.name, "path": str(pth.resolve()), "sha256": digest, "source": "file", "manifest": lab,
                    "in_manifest": bool(lab), "reason": reason}
        if not imgs:
            raise CmdError("this step lists no final image: use Open image...", 422)
        i = choice.get("index")
        try:
            im = imgs[int(i)] if i is not None else self.default_image(s)
        except (IndexError, ValueError, TypeError):
            raise CmdError(f"no final image number {i!r} in this protocol ({len(imgs)} listed): choose the image again", 422)
        pth = Path(str(im["path"])).expanduser()
        if not pth.is_file():
            raise CmdError(f"image file not found: {pth}", 404)
        digest = sha256_of(pth)
        if im.get("sha256") and str(im["sha256"]).lower() != digest:
            raise CmdError(f"'{im['label']}' differs from the hash in the protocol; rebuild or update the protocol", 409)
        return {"label": im["label"], "path": str(pth.resolve()), "sha256": digest, "source": "protocol",
                "manifest": known.get(digest), "in_manifest": digest in known, "reason": ""}

    def image_preview(self, step_id, path):
        s = self._step(step_id)
        pth = Path(str(path)).expanduser()
        if pth.suffix.lower() not in IMAGE_EXTS:
            raise CmdError("choose a .hex, .bin or .elf file", 422)
        if not pth.is_file():
            raise CmdError(f"not a file: {pth}", 404)
        digest = sha256_of(pth)
        known = manifest_labels(s["manifest"]) if s.get("manifest") else {}
        for im in s.get("final_images") or []:
            if im.get("sha256"):
                known.setdefault(str(im["sha256"]).lower(), im.get("label", ""))
        lab = known.get(digest)
        fin = final_action(s)
        warn = ""
        if pth.suffix.lower() == ".bin" and fin and fin["plugin"].startswith("openocd."):
            warn = "openocd.flash cannot flash a .bin (no load address): use the .hex or the ELF"
        return {"ok": True, "name": pth.name, "path": str(pth.resolve()), "sha256": digest, "size": pth.stat().st_size,
                "match": bool(lab), "manifest_label": lab, "manifest": s.get("manifest"), "warning": warn}

    # ---- the operator's two ways out of the verdict
    def _record(self, board, step_id):
        rec = self.engine.state.sequences.get((board, step_id))
        if not rec or rec.get("outcome") in ("running", None):
            raise CmdError("no finished run for this board", 409)
        if rec.get("proceed") or rec.get("defects"):
            raise CmdError("this run's verdict was already recorded; open a retest to run again", 409)
        return rec

    async def proceed(self, step_id, board, confirm=False, image=None):
        s = self._step(step_id)
        eng = self.engine
        rec = self._record(board, step_id)
        if rec["outcome"] != "complete":
            raise CmdError(f"the run {rec['outcome']}: it cannot proceed. Record defects or diagnose", 409)
        if self.active or self.runner.current:
            raise CmdError("something is already running", 409)
        fin = final_action(s)
        sk = (rec.get("bench") or {}).get("skips") or {}
        if fin and [n for n in fin.get("needs") or [] if n in sk]:
            fin = None                                  # the final flash needs equipment the operator skipped: nothing to flash
        img = self.image_info(s, image) if fin else None
        if img and img["path"].lower().endswith(".bin") and fin["plugin"].startswith("openocd."):
            raise CmdError("openocd.flash cannot flash a .bin (no load address): use the .hex or the ELF", 422)
        r = eng.seq_accept(board, step_id, "proceed", confirm=confirm)
        eng.seq_event(board, step_id, "proceed", seq=rec["seq"], attempt=rec["attempt"], accepted=r["accepted"], confirmed=r["confirmed"],
                      image=img, snapshot=self._snapshot(board, step_id))
        if fin is None:
            await self.broadcast({"kind": "state"})
            return {"ok": True, **r, "final": None}
        self._start_final(s, board, rec, fin, img)
        await self._push()
        return {"ok": True, **r, "final": "started"}

    def _start_final(self, s, board, rec, fin, img):
        self.active, self.title, self.board, self._abort = True, s["title"], board, False
        plan = next((p for p in rec["plan"] if p["final"]), {"id": fin["id"], "title": fin.get("title", fin["id"]), "kind": "action", "final": True, "proposes": fin.get("proposes", [])})
        self.live = {"seq": rec["seq"], "step": s["id"], "board": board, "phase": "final", "t0": time.time(), "attempt": rec["attempt"],
                     "items": [{**plan, "state": "running", "dur_s": None, "run": None, "t0": time.time(), "answer": None}], "ask": None, "image": img}
        self._task = asyncio.create_task(self._run_final(s, board, fin, img))

    def _final_tries(self, board, step_id, seq) -> int:
        return sum(1 for e in self.engine.journal.events if e["type"] == "sequence" and e["payload"].get("phase") == "final"
                   and e["payload"].get("board") == board and e["payload"].get("step") == step_id and e["payload"].get("seq") == seq)

    def _snapshot(self, board, step_id) -> dict:
        return {r["test"]: [r["value"], r["unit"], r["verdict"], list(r["flags"] or [])] for r in self.engine.seq_rows(board, step_id)
                if not r.get("final") and r["src"] == "accepted"}

    def _changed_since_proceed(self, board, step_id, rec) -> list[str]:
        """Tests whose accepted value or verdict differs from what the operator accepted at the proceed; for a journal without that
        snapshot, any accepted test that is not clean and was not confirmed at the proceed."""
        snap = None
        for e in reversed(self.engine.journal.events):
            p = e["payload"]
            if e["type"] == "sequence" and p.get("phase") == "proceed" and p.get("board") == board and p.get("step") == step_id and p.get("seq") == rec["seq"]:
                snap = p.get("snapshot")
                break
        now = self._snapshot(board, step_id)
        if snap is None:
            conf = set((rec.get("proceed") or {}).get("confirmed") or [])
            rows = {r["test"]: r for r in self.engine.seq_rows(board, step_id)}
            return [t for t in now if not rows[t]["clean"] and t not in conf]
        return sorted(t for t in set(now) | set(snap) if json.dumps(now.get(t), default=str) != json.dumps(snap.get(t), default=str))

    async def retry_final(self, step_id, board, image=None):
        """UIR-01-R1: the final flash failed after the verdict was accepted. Run only that action again: no new sequence, no new attempt,
        the accepted results stay. The image is the one the operator chose before (re-hashed), unless a new choice is given."""
        s = self._step(step_id)
        eng = self.engine
        eng._board(board)
        eng._check_open(board, s)
        if self.active or self.runner.current:
            raise CmdError("something is already running; wait for it or abort it", 409)
        rec = eng.state.sequences.get((board, step_id))
        fin = final_action(s)
        if not rec or not rec.get("proceed") or not fin:
            raise CmdError("no accepted run is waiting for its final flash on this board", 409)
        if rec.get("attempt") != eng.state.attempt(board, step_id):
            raise CmdError(f"this board was reopened as attempt {eng.state.attempt(board, step_id)}; the accepted run belongs to attempt "
                           f"{rec.get('attempt')}. Run the sequence again", 409)
        if (rec.get("final") or {}).get("state") == "ok":
            raise CmdError("the final image is already programmed", 409)
        changed = self._changed_since_proceed(board, step_id, rec)
        if changed:
            raise CmdError("the accepted results changed after the verdict (" + ", ".join(changed[:6]) + "): the verdict is no longer the one "
                           "that was accepted. Run again or record defects; do not flash on it", 409, changed=changed)
        if rec.get("final") is None and not rec.get("image"):
            raise CmdError("nothing was flashed or chosen for this run", 409)
        sk = (rec.get("bench") or {}).get("skips") or {}
        if [n for n in fin.get("needs") or [] if n in sk]:
            raise CmdError("the final flash needs equipment that was skipped for this run", 409)
        if image is None:
            im = rec.get("image") or {}
            if im.get("source") == "file":
                image = {"source": "file", "path": im.get("path"), "reason": im.get("reason", "")}
            else:
                idx = next((i for i, x in enumerate(s.get("final_images") or []) if x.get("label") == im.get("label")), None)
                if idx is None:
                    raise CmdError(f"the image '{im.get('label')}' is no longer listed in the protocol: choose the image again", 422)
                image = {"source": "protocol", "index": idx}
        img = self.image_info(s, image)
        if img["path"].lower().endswith(".bin") and fin["plugin"].startswith("openocd."):
            raise CmdError("openocd.flash cannot flash a .bin (no load address): use the .hex or the ELF", 422)
        self._start_final(s, board, rec, fin, img)
        await self._push()
        return {"ok": True, "final": "started", "retry": self._final_tries(board, step_id, rec["seq"]) + 1, "attempt": rec["attempt"]}

    async def _run_final(self, s, board, fin, img):
        it = self.live["items"][0]
        seq, att = self.live["seq"], self.live["attempt"]
        state, text, run = "error", "", None
        try:
            r = await self.runner.start(s["id"], SEQ_SECTION, fin["id"], board, True, params_override={"image": img["path"]}, from_sequence=True)
            run = r.get("run")
            res = await self.runner.wait(run) if r.get("ok") else {"state": "error", "text": "could not start"}
            state, text = res["state"], res.get("text", "")
        except CmdError as e:
            state, text = "error", f"the final image was not flashed: {e}"
        except asyncio.CancelledError:
            state, text = "cancelled", "aborted"
        dur = round(time.time() - it["t0"], 1)
        if state == "error":
            dropped = self._bench_cache.pop(s["id"], None)  # a failed flash is usually the probe: the next sequence looks at the bench again
            if dropped:
                self._bench_stash[s["id"]] = dropped
        elif state == "ok":
            kept = self._bench_stash.pop(s["id"], None)     # the programmer just worked: the check that was dropped for it stands again
            if kept and kept["sig"] == self._sig([e for e in s.get("equipment") or [] if isinstance(e, dict)]):
                fp = await asyncio.to_thread(self.usb_fingerprint)
                self._bench_cache[s["id"]] = {**kept, "ts": time.time(), "fp": fp}
        self.engine.seq_event(board, s["id"], "final", seq=seq, attempt=att, state=state, run=run, text=text, dur_s=dur, image=img,
                              try_no=self._final_tries(board, s["id"], seq) + 1)
        self.active = False
        self.live = None
        await self._push()
        self._notify("final", {"board": board, "step": s["id"], "state": state, "text": text})

    async def defects(self, step_id, board, note):
        s = self._step(step_id)
        rec = self._record(board, step_id)
        if self.active or self.runner.current:
            raise CmdError("something is already running", 409)
        rows = self.engine.seq_rows(board, step_id)
        bad = [r["test"] for r in rows if not r.get("final") and r["src"] not in ("none", "skipped") and not r["clean"]]   # 19 Oct: real failures only
        not_run = sum(1 for r in rows if not r.get("final") and r["src"] == "none")
        r = self.engine.seq_accept(board, step_id, "defects", note=note)
        self.engine.seq_event(board, step_id, "defects", seq=rec["seq"], attempt=rec["attempt"], tests=bad, not_run=not_run, note=str(note).strip())
        await self.broadcast({"kind": "state"})
        return {"ok": True, **r, "defects": bad}
