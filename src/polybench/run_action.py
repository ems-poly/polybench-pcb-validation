"""Child process that runs one action. Spoken protocol: JSON lines on stdout.

  python -m polybench.run_action <plugin> '<json args>'            run
  python -m polybench.run_action --safe-state <plugin> '<json>'    call safe_state() only

Out : {"t":"proposal"|"log"|"progress"|"ask"|"done"|"error", ...}
In  : {"t":"cancel"} or {"t":"answer","value":"..."}  (one JSON line each)
The parent never kills this process; the plugin owns its timeouts.
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import traceback
from pathlib import Path

from .extras import MissingExtra
from .plugin_api import ActionContext, Cancelled  # Cancelled is defined in plugin_api, never here (see its docstring)
from .plugins._util import PluginError


def _table_value(workdir, rows, columns, csv_name):
    """The table value of a proposal: {columns, rows[, attachment]}. rows may be lists or dicts; csv= reads a CSV file in the run folder
    (first line = column ids) and keeps its name as the attachment."""
    import csv as _csv
    att = None
    if csv_name:
        att = str(csv_name)
        with open(Path(workdir) / att, newline="", encoding="utf-8") as f:
            rd = list(_csv.reader(f))
        columns = [c.strip() for c in rd[0]]
        rows = [[_num(x) for x in r] for r in rd[1:] if r]
    elif rows and isinstance(rows[0], dict):
        columns = columns or list(rows[0])
        rows = [[r.get(c) for c in columns] for r in rows]
    v = {"rows": [list(r) for r in rows or []]}
    if columns:
        v["columns"] = list(columns)
    if att:
        v["attachment"] = att
    return v


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return x


class ChildContext(ActionContext):
    def __init__(self, out, board, params, workdir, simulate):
        super().__init__(board=board, params=params, workdir=Path(workdir), simulate=simulate)
        self._out = out
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._answers: queue.Queue = queue.Queue()
        threading.Thread(target=self._stdin_reader, daemon=True).start()

    def _send(self, obj):
        with self._lock:
            self._out.write(json.dumps(obj) + "\n")
            self._out.flush()

    def _stdin_reader(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("t") == "cancel":
                self._cancel.set()
                self._answers.put(None)
            elif m.get("t") == "answer":
                self._answers.put(m.get("value"))
        # parent closed stdin (it died): treat as cancel so we do not run unattended
        self._cancel.set()
        self._answers.put(None)

    def propose(self, test_id, value, unit=None, evidence=None, raw=None):
        self._send({"t": "proposal", "test_id": test_id, "value": str(value), "unit": unit,
                    "evidence": evidence, "raw": raw})

    def propose_table(self, test_id, rows=None, columns=None, csv=None, evidence=None, raw=None):
        self._send({"t": "proposal", "test_id": test_id, "value": _table_value(self.workdir, rows, columns, csv),
                    "unit": None, "evidence": evidence, "raw": raw})

    def artifact(self, kind, data):
        """A result file or record the core should journal (e.g. the AOI candidate list). Large payloads are fine."""
        self._send({"t": "artifact", "kind": kind, "data": data})

    def analysis(self, analysis, samples=None):
        self._send({"t": "analysis", "analysis": analysis, "samples": list(samples or [])})

    def analysis(self, analysis, samples=None):
        self._send({"t": "analysis", "analysis": analysis, "samples": list(samples or [])})

    def log(self, text, level="info"):
        self._send({"t": "log", "text": str(text), "level": level})

    def progress(self, fraction, text=""):
        self._send({"t": "progress", "fraction": float(fraction), "text": text})

    def cancelled(self):
        return self._cancel.is_set()

    def ask(self, prompt, options):
        self._send({"t": "ask", "prompt": prompt, "options": list(options)})
        v = self._answers.get()
        if v is None:
            raise Cancelled("cancelled while waiting for the operator")
        return v


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    safe = False
    if argv and argv[0] == "--safe-state":
        safe = True
        argv.pop(0)
    plugin, args = argv[0], json.loads(argv[1] if len(argv) > 1 else "{}")
    for stream in (sys.stdin, sys.stdout, sys.stderr):      # UTF-8 on every OS: Windows pipes default to the legacy code page
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    out = sys.stdout
    sys.stdout = sys.stderr  # stray prints from plugins must not corrupt the protocol
    from . import registry
    try:
        action = registry.find_action(plugin)
        if safe:
            text = action.safe_state()
            out.write(json.dumps({"t": "done", "text": str(text)}) + "\n")
            out.flush()
            return 0
        ctx = ChildContext(out, args.get("board", ""), args.get("params", {}), args.get("workdir", "."),
                           bool(args.get("simulate")))
        Path(ctx.workdir).mkdir(parents=True, exist_ok=True)
        action.run(ctx)
        ctx._send({"t": "done", "cancelled": ctx.cancelled()})
        return 0
    except Cancelled as e:
        out.write(json.dumps({"t": "done", "cancelled": True, "text": str(e)}) + "\n")
        out.flush()
        return 0
    except BaseException as e:  # noqa: BLE001  report everything, including plugin bugs
        tb = traceback.format_exc()
        if isinstance(e, (PluginError, MissingExtra)):
            # an expected failure whose message already tells the operator what to do: the traceback goes to the debug log only
            try:
                dbg = Path(args.get("workdir", ".")) / "action_debug.log"
                dbg.parent.mkdir(parents=True, exist_ok=True)
                with open(dbg, "a", encoding="utf-8") as f:
                    f.write(tb)
            except OSError:
                pass
        else:
            sys.stderr.write(tb)
        out.write(json.dumps({"t": "error", "text": str(e) if isinstance(e, MissingExtra) else f"{type(e).__name__}: {e}"}) + "\n")
        out.flush()
        return 1


if __name__ == "__main__":
    sys.exit(main())
