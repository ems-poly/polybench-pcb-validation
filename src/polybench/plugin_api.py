from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class Cancelled(Exception):
    """Raised by a plugin (or ctx.ask) when the operator cancels. Lives here, a module that is imported and never run:
    under `python -m polybench.run_action` that module loads as __main__, so a class defined there is a different class
    from the one plugins import."""


@dataclass
class ActionContext:
    board: str                 # board label, e.g. "S03"
    params: dict               # from the protocol action
    workdir: Path              # run folder for evidence files
    simulate: bool
    def propose(self, test_id: str, value: str, unit: str | None = None,
                evidence: str | None = None, raw: str | None = None) -> None: ...
    def propose_table(self, test_id: str, rows: list | None = None, columns: list[str] | None = None,
                      csv: str | None = None, evidence: str | None = None, raw: str | None = None) -> None: ...
    # a result for a `kind: table` test: rows = [[...], ...] or [{col: v}, ...]; columns = column ids (default: the test's own, in order);
    # csv = name of a CSV file in ctx.workdir (header line = column ids), kept as the cell's attachment
    def log(self, text: str, level: str = "info") -> None: ...
    def artifact(self, kind: str, data: dict) -> None: ...   # record for the journal (aoi.run: the candidate list)
    def analysis(self, analysis: dict, samples: list[dict] | None = None) -> None: ...
    # what the action measured, for the Results explorer and for `derive` rules in the protocol: `analysis` is a flat-ish dict
    # (min, max, mean, final, tripped, settled, ...), `samples` rows {t_s, channel, quantity, value, unit, mode?, test?}.
    # The core stores it as measurements.json in the run folder and turns each `derive` rule of the action into a proposal.
    def analysis(self, analysis: dict, samples: list[dict] | None = None) -> None: ...
    # what the action measured, for the Results explorer and for `derive` rules in the protocol: `analysis` is a flat-ish dict
    # (min, max, mean, final, tripped, settled, ...), `samples` rows {t_s, channel, quantity, value, unit, mode?, test?}.
    # The core stores it as measurements.json in the run folder and turns each `derive` rule of the action into a proposal.
    def progress(self, fraction: float, text: str = "") -> None: ...
    def cancelled(self) -> bool: ...
    def ask(self, prompt: str, options: list[str]) -> str: ...   # operator prompt, e.g. "Connect bridge simulator at 2 mV/V"

class Action(Protocol):
    name: str                  # e.g. "openocd.flash"
    capabilities: list[str]
    def run(self, ctx: ActionContext) -> None: ...    # raises on failure; never computes verdicts
    def safe_state(self) -> str: ...                 # outputs off / bus released, read back where possible
    def selftest(self) -> bool: ...                  # no hardware

class EcadBridge(Protocol):
    name: str                  # e.g. "fusion"
    def status(self) -> dict: ...                    # {connected: bool, program, document, detail}
    def highlight(self, designators: list[str]) -> dict: ...
    def clear(self) -> None: ...
    def designators(self) -> list[str] | None: ...   # for cross-checking the BOM, if available
