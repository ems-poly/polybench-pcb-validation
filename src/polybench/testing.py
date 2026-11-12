"""Helpers for plugin authors: a recording ActionContext that needs no child process."""
from __future__ import annotations

from pathlib import Path

from .plugin_api import ActionContext


class RecordingContext(ActionContext):
    """Collects proposals, logs and progress; answers ask() from `answers` (default: first option)."""

    def __init__(self, board="S01", params=None, workdir=None, simulate=True, answers=None):
        super().__init__(board=board, params=params or {}, workdir=Path(workdir or "."), simulate=simulate)
        self.proposals, self.logs, self.progresses, self.asked = [], [], [], []
        self.analyses: list[dict] = []          # ctx.analysis(...) calls: {analysis, samples}
        self.answers = list(answers or [])
        self._cancel = False

    def propose(self, test_id, value, unit=None, evidence=None, raw=None):
        self.proposals.append({"test_id": test_id, "value": value, "unit": unit, "evidence": evidence, "raw": raw})

    def propose_table(self, test_id, rows=None, columns=None, csv=None, evidence=None, raw=None):
        from .run_action import _table_value
        self.proposals.append({"test_id": test_id, "value": _table_value(self.workdir, rows, columns, csv), "unit": None,
                               "evidence": evidence, "raw": raw, "table": True})

    def analysis(self, analysis, samples=None):
        self.analyses.append({"analysis": analysis, "samples": list(samples or [])})

    def log(self, text, level="info"):
        self.logs.append((level, text))

    def progress(self, fraction, text=""):
        self.progresses.append((fraction, text))

    def cancelled(self):
        return self._cancel

    def ask(self, prompt, options):
        self.asked.append((prompt, options))
        return self.answers.pop(0) if self.answers else (options[0] if options else "")
