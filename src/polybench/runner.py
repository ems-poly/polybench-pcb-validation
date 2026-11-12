"""Runs actions as child processes and relays their JSON lines. Never kills anything it did not need to.

One action at a time. Abort = {"t":"cancel"} on stdin, wait for the child (no timer), then safe_state()
in a fresh child. The plugin owns its timeouts.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import deque
from pathlib import Path

from . import derive as _derive
from .engine import CmdError, Engine


class ActionRunner:
    def __init__(self, engine: Engine, batch_dir: Path, broadcast, simulate: bool):
        self.engine = engine
        self.batch_dir = Path(batch_dir)
        self.broadcast = broadcast  # async callable(dict)
        self.simulate = simulate
        self.console: deque = deque(maxlen=4000)
        self.current: dict | None = None
        self._proc = None
        self._run_seq = sum(1 for e in engine.journal.events if e["type"] == "action_start")
        self.last_plugins: dict[str, dict] = {}
        self.meter = None                            # the live MeterService: paused while a dmm.* action holds the cable (one owner of the HID handle)
        self._meter_paused = False
        self.seq = None                              # the SequenceRunner (sequence.py), when one exists
        self._events: dict[int, asyncio.Event] = {}  # run -> set when the run has ended
        self.results: dict[int, dict] = {}           # run -> {state, text} of the ended runs the sequence asked about

    # ---- helpers
    async def _line(self, text, level="info", run=0):
        rec = {"ts": time.strftime("%H:%M:%S"), "level": level, "text": text, "run": run}
        self.console.append(rec)
        await self.broadcast({"kind": "console", **rec})

    async def _status(self):
        cur = None
        if self.current:
            cur = {k: v for k, v in self.current.items() if k not in ("proc",)}
        await self.broadcast({"kind": "action", "current": cur})

    def status(self):
        if self.current:
            return {k: v for k, v in self.current.items() if k != "proc"}
        if self.seq is not None and self.seq.active:        # between two actions of a sequence the bench is still busy
            return self.seq.gap_status()
        return None

    async def wait(self, run):
        """The result {state, text} of a run once it has ended (the sequence awaits each action this way)."""
        ev = self._events.get(run)
        if ev is not None:
            await ev.wait()
        self._events.pop(run, None)
        return self.results.pop(run, {"state": "error", "text": "no result"})

    def find_action(self, step_id, section_id, action_id):
        s = self.engine.proto.step(step_id)
        sec = next((x for x in (s or {}).get("sections", []) if x["id"] == section_id), None)
        a = next((x for x in (sec or {}).get("actions", []) if x["id"] == action_id), None)
        if not a:
            raise CmdError("unknown action", 404)
        return s, a

    # ---- run
    async def start(self, step_id, section_id, action_id, board, confirmed=False, params_override=None, from_sequence=False):
        if self.seq is not None and self.seq.active and not from_sequence:
            raise CmdError(f"the {self.seq.title} sequence is running on {self.seq.board}; wait for it or abort it", 409)
        if self.current:
            c = self.current
            raise CmdError(f"'{c['title']}' is already running on {c['board']}; wait for it or abort it", 409)
        s, a = self.find_action(step_id, section_id, action_id)
        rec = self.engine.state.sequences.get((board, step_id))
        sk = ((rec or {}).get("bench") or {}).get("skips") or {}
        gone = [n for n in a.get("needs") or [] if n in sk]
        if gone:
            raise CmdError(f"'{a['title']}' needs equipment you skipped ({', '.join(gone)}): {sk[gone[0]]}", 409)
        self.engine._board(board)
        self.engine._check_open(board, s)
        prompt = str((a.get("params") or {}).get("prompt") or "").strip()
        if prompt and not confirmed:
            return {"ok": True, "needs_prompt": prompt, "title": a["title"], "board": board}
        if prompt:
            self.engine._emit("action_confirm", {"board": board, "action": action_id, "step": step_id, "prompt": prompt})
            await self._line(f"operator confirmed on {board}: {prompt}", "ask", 0)
        self._run_seq += 1
        run = self._run_seq
        workdir = self.batch_dir / "runs" / f"{run:04d}_{board}_{a['id']}"
        workdir.mkdir(parents=True, exist_ok=True)
        params = dict(a.get("params", {}))
        if params_override:
            params.update(params_override)
        if a["plugin"].startswith("aoi."):
            params["_aoi"] = self.engine.proto.data.get("aoi")
        args = {"board": board, "params": params, "workdir": str(workdir), "simulate": self.simulate}
        self._events[run] = asyncio.Event()
        self.current = {"run": run, "plugin": a["plugin"], "board": board, "step": step_id, "section": section_id,
                        "action": action_id, "title": a["title"], "fraction": 0.0, "text": "starting", "ask": None,
                        "state": "running", "proposes": a.get("proposes", [])}
        self.last_plugins[a["plugin"]] = args
        self.engine._emit("action_start", {"run": run, "plugin": a["plugin"], "board": board, "action": action_id,
                                           "step": step_id, "params": {**a.get("params", {}), **(params_override or {})}, "simulate": self.simulate,
                                           "workdir": str(workdir)})
        await self._line(f"--- run {run}: {a['title']} ({a['plugin']}) on {board}", "head", run)
        await self.broadcast({"kind": "state"})
        await self._status()
        if a["plugin"].startswith("dmm.") and self.meter is not None and not self.simulate:
            self._meter_paused = True
            if not await asyncio.to_thread(self.meter.pause):
                await self._line("the live meter reader did not release the cable in time; the action may find it busy", "warn", run)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "polybench.run_action", a["plugin"], json.dumps(args),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=16 * 1024 * 1024)
        except Exception as e:  # cannot even spawn
            await self._finish(run, "error", f"could not start child: {e}")
            return {"ok": False, "run": run}
        asyncio.create_task(self._pump(run, a, board))
        return {"ok": True, "run": run}

    async def _pump(self, run, a, board):
        proc = self._proc
        result = {"state": "ok", "text": ""}
        sim = self.simulate or a["plugin"].startswith("demo.")
        logf = open(self.batch_dir / "runs" / f"{run:04d}_{board}_{a['id']}" / "console.log", "a", buffering=1)

        async def err_reader():
            async for raw in proc.stderr:
                t = raw.decode(errors="replace").rstrip()
                if t:
                    logf.write("[stderr] " + t + "\n")
                    await self._line(t, "stderr", run)

        errt = asyncio.create_task(err_reader())
        try:
            async for raw in proc.stdout:
                text = raw.decode(errors="replace").strip()
                if not text:
                    continue
                try:
                    m = json.loads(text)
                    t = m.get("t")
                except ValueError:
                    logf.write(text + "\n")
                    await self._line(text, "info", run)
                    continue
                if t == "log":
                    logf.write(f"[{m.get('level', 'info')}] {m.get('text')}\n")
                    await self._line(m.get("text", ""), m.get("level", "info"), run)
                elif t == "progress":
                    self.current.update(fraction=float(m.get("fraction", 0)), text=m.get("text", ""))
                    await self._status()
                elif t == "proposal":
                    tid = m.get("test_id")
                    if tid not in self.engine.state.test_index:
                        await self._line(f"proposal for unknown test '{tid}' dropped", "warn", run)
                        continue
                    if tid not in a.get("proposes", []):
                        await self._line(f"note: {tid} is not in this action's 'proposes' list", "warn", run)
                    r = self.engine.add_proposal(board, tid, m.get("value", ""), m.get("unit"), m.get("evidence"), m.get("raw"),
                                                 plugin=a["plugin"], run=run, simulate=sim, record=a.get("record", "auto"))
                    shown = f"table, {len(m['value'].get('rows', []))} rows" if isinstance(m.get("value"), dict) else f"{m.get('value')} {m.get('unit') or ''}"
                    how = {"recorded": "recorded", "replaced": "recorded (replaced the earlier value of this test; the earlier value stays in its history)"}.get(r.get("recorded"), "proposed" + (f" ({r['why']})" if r.get("why") else ""))
                    logf.write(f"[{r.get('recorded')}] {tid}={shown}\n")
                    await self._line(f"{tid} = {shown}: {how}", "proposal", run)
                    await self.broadcast({"kind": "state"})
                elif t == "analysis":
                    await self._on_analysis(run, a, board, m, sim, logf)
                elif t == "artifact":
                    if m.get("kind") == "aoi_run":
                        self.engine.aoi_run_done(board, {**m["data"], "run": run})
                        await self._line(f"AOI result recorded: {len(m['data'].get('items', []))} candidates", "info", run)
                        await self.broadcast({"kind": "state"})
                elif t == "ask":
                    self.current["ask"] = {"prompt": m.get("prompt", ""), "options": m.get("options", [])}
                    await self._line(f"? {m.get('prompt')}  {m.get('options')}", "ask", run)
                    await self._status()
                elif t == "done":
                    if m.get("cancelled"):
                        result["state"] = "cancelled"
                    result["text"] = m.get("text", "")
                elif t == "error":
                    result = {"state": "error", "text": m.get("text", "")}
                    await self._line("ERROR: " + m.get("text", ""), "error", run)
        finally:
            rc = await proc.wait()
            await errt
            logf.close()
        if rc != 0 and result["state"] == "ok":
            result = {"state": "error", "text": f"child exited with code {rc}"}
            await self._line(result["text"], "error", run)
        await self._finish(run, result["state"], result["text"])

    async def _on_analysis(self, run, a, board, m, sim, logf):
        """What the action measured: stored with the run (the Results explorer reads it), then each `derive` rule of the action
        becomes a proposal for its test, with the analysis value as evidence."""
        wd = self.batch_dir / "runs" / f"{run:04d}_{board}_{a['id']}"
        analysis, samples = m.get("analysis") or {}, m.get("samples") or []
        derived, skipped = _derive.derive_values(a.get("derive") or [], analysis)
        try:
            (wd / "measurements.json").write_text(json.dumps({"action": a["id"], "plugin": a["plugin"], "board": board, "run": run,
                                                              "analysis": analysis, "samples": samples, "derived": derived}, indent=1))
        except OSError as e:
            await self._line(f"could not store the measurements: {e}", "warn", run)
        for why in skipped:
            await self._line(f"not derived: {why}", "warn", run)
        for d in derived:
            tid = d["test"]
            if tid not in self.engine.state.test_index:
                await self._line(f"derived value for unknown test '{tid}' dropped", "warn", run)
                continue
            raw = f"derived from {d['source_path']} = {d['source_value']}" + (f" ({d['note']})" if d["note"] else "")
            r = self.engine.add_proposal(board, tid, d["value"], d["unit"], str(wd / "measurements.json"), raw,
                                         plugin=a["plugin"], run=run, simulate=sim, record=a.get("record", "auto"))
            how = {"recorded": "recorded", "replaced": "recorded (replaced the earlier value)"}.get(r.get("recorded"), "proposed" + (f" ({r['why']})" if r.get("why") else ""))
            logf.write(f"[{r.get('recorded')}] {tid}={d['value']} (derived)\n")
            await self._line(f"{tid} = {d['value']}: {how}; {raw}", "proposal", run)
        if derived:
            await self.broadcast({"kind": "state"})

    async def _finish(self, run, state, text):
        cancelled = self.current and self.current.get("cancel_requested")
        plugin = self.current["plugin"] if self.current else ""
        if cancelled and state == "ok":
            state = "cancelled"
        self.engine._emit("action_end", {"run": run, "state": state, "text": text})
        self.results[run] = {"state": state, "text": text}
        await self._line(f"--- run {run} {state}" + (f": {text}" if text else ""), "head" if state == "ok" else "warn", run)
        if state in ("cancelled",) or cancelled:
            await self._safe(plugin, run)
        self.current = None
        self._proc = None
        if self._meter_paused:
            self._meter_paused = False
            try:
                self.meter.resume()
            except Exception:
                pass
        ev = self._events.get(run)
        if ev is not None:
            ev.set()
        await self._status()
        await self.broadcast({"kind": "state"})

    async def answer(self, value):
        if not self.current or not self.current.get("ask") or not self._proc:
            raise CmdError("no question is waiting", 409)
        self.current["ask"] = None
        self._proc.stdin.write((json.dumps({"t": "answer", "value": value}) + "\n").encode())
        await self._proc.stdin.drain()
        await self._line(f"> answered: {value}", "ask", self.current["run"])
        await self._status()

    async def cancel(self):
        if not self.current and self.seq is not None and self.seq.active:
            self.seq.request_abort()                          # between two actions, or at an operator question
            return {"ok": True}
        if not self.current or not self._proc:
            raise CmdError("nothing is running", 409)
        if self.seq is not None and self.seq.active:
            self.seq.request_abort()
        self.current["cancel_requested"] = True
        self.current["state"] = "cancelling"
        self.current["ask"] = None
        try:
            self._proc.stdin.write((json.dumps({"t": "cancel"}) + "\n").encode())
            await self._proc.stdin.drain()
        except Exception:
            pass
        await self._line("abort requested: waiting for the plugin to stop (no timer, the plugin owns its timeouts)", "warn", self.current["run"])
        await self._status()
        return {"ok": True}

    async def _safe(self, plugin, run=0):
        args = self.last_plugins.get(plugin, {"board": "", "params": {}, "workdir": ".", "simulate": self.simulate})
        text = ""
        try:
            p = await asyncio.create_subprocess_exec(sys.executable, "-m", "polybench.run_action", "--safe-state", plugin,
                                                     json.dumps(args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await p.communicate()
            for line in out.decode(errors="replace").splitlines():
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                if m.get("t") == "done":
                    text = m.get("text", "")
                if m.get("t") == "error":
                    text = "safe_state ERROR: " + m.get("text", "")
        except Exception as e:
            text = f"safe_state could not run: {e}"
        self.engine._emit("safe_state", {"plugin": plugin, "run": run, "text": text})
        await self._line(f"safe state ({plugin}): {text}", "warn", run)
        return text

    async def safe_state_all(self):
        if self.current:
            raise CmdError("an action is running; abort it first", 409)
        plugins = sorted({a["plugin"] for s in self.engine.proto.data["steps"] for sec in s.get("sections", [])
                          for a in sec.get("actions", [])})
        res = {}
        for pl in plugins:
            res[pl] = await self._safe(pl)
        return res
