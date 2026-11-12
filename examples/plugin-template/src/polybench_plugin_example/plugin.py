"""Action `example.echo`: proposes a fixed text and a number. HOST-ONLY (no hardware, nothing to validate).

params: value (3.3), unit ("V"), map {reading} -> the test id that receives the value (set in the protocol, never here).
"""
from __future__ import annotations

from pathlib import Path


class ExampleEcho:
    name = "example.echo"
    capabilities: list[str] = []

    def run(self, ctx) -> None:
        # A real plug-in opens its instrument here (once), reads, and closes it in `finally`. With ctx.simulate it must
        # not touch hardware or the network; this example never does.
        value = float(ctx.params.get("value", 3.3))
        unit = str(ctx.params.get("unit", "V"))
        if ctx.cancelled():
            ctx.log("cancelled", "warn")
            return
        out = Path(ctx.workdir) / f"{ctx.board}_echo_{'sim' if ctx.simulate else 'run'}.txt"
        out.write_text(f"{value} {unit}\n")
        test_id = (ctx.params.get("map") or {}).get("reading")
        if test_id:                                   # no verdicts: only the value, unit, evidence file and raw text
            ctx.propose(test_id, str(value), unit, str(out), f"{value} {unit}")
        ctx.log(f"echoed {value} {unit}")

    def safe_state(self) -> str:
        return "example.echo holds nothing; no outputs to turn off"    # no arguments, fresh child, no run state

    def selftest(self) -> bool:
        return float("3.3") == 3.3        # logic only, no hardware
