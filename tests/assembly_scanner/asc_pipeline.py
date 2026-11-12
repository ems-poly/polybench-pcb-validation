"""Test-side reference glue for decode -> resolve -> debounce -> Fusion, per docs/CONTRACTS.md.

The contract defines the four modules but not the glue (that lives in the scanner agent's
ui/main). This harness composes the *real* modules exactly as the UX rules say:

  - a fired MATCH            -> client.clear(), then client.highlight(designators)
  - a fired MATCH_NO_PLACEMENT / NOT_IN_BOM / NO_CANDIDATE -> no Fusion call (UI status only)
  - nothing in view          -> no Fusion call (the highlight stays)

so the scenario tests check that resolver + Debouncer + FusionClient together satisfy the
rules. If the app later exposes its own controller, point `Pipeline` at it instead.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from asc_support import need

FPS = 30.0


@dataclass
class SpyFusion:
    """Records FusionClient method calls; optionally forwards to a real client."""
    inner: Any = None
    log: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)

    def highlight(self, designators):
        self.log.append(("highlight", tuple(designators)))
        return self.inner.highlight(list(designators)) if self.inner else None

    def clear(self):
        self.log.append(("clear", ()))
        return self.inner.clear() if self.inner else None

    @property
    def highlights(self):
        return [d for op, d in self.log if op == "highlight"]

    @property
    def current(self) -> tuple[str, ...] | None:
        """What Fusion shows now, replaying the log (clear -> None, highlight -> set)."""
        cur = None
        for op, d in self.log:
            cur = None if op == "clear" else d
        return cur


@dataclass
class Tick:
    t: float
    codes: tuple[str, ...]
    resolution: Any
    fired: Any
    fusion_ops: int


class Pipeline:
    def __init__(self, bom, client, decode: Callable | None = None, debouncer=None):
        resolver = need("polybench.assembly_scanner.resolver")
        state = need("polybench.assembly_scanner.state")
        self.models = need("polybench.assembly_scanner.models")
        self.resolve = lambda codes: resolver.resolve(list(codes), bom)
        self.decode = decode
        self.deb = debouncer or state.Debouncer()
        self.client = client
        self.fired: list = []
        self.ticks: list[Tick] = []

    def step_codes(self, codes: Iterable[str], now: float):
        codes = tuple(codes)
        res = self.resolve(codes) if codes else None
        out = self.deb.update(res, now)
        n_before = len(getattr(self.client, "log", []))
        if out is not None:
            self.fired.append(out)
            if out.status == self.models.Status.MATCH:
                self.client.clear()
                self.client.highlight(list(out.designators))
        self.ticks.append(Tick(now, codes, res, out, len(getattr(self.client, "log", [])) - n_before))
        return out

    def step_frame(self, frame, now: float):
        codes = self.decode(frame) if frame is not None else []
        return self.step_codes(codes, now)


def run_stream(pipe: Pipeline, segments, t0: float = 0.0, fps: float = FPS) -> float:
    """segments: [(codes_or_None, seconds), ...]. Returns the end time."""
    t = t0
    dt = 1.0 / fps
    for codes, dur in segments:
        n = int(round(dur * fps))
        for _ in range(n):
            pipe.step_codes(codes or (), t)
            t += dt
    return t


# --- synthetic label code sets (what scanner.decode returns for a Mouser bag label) -------
# Mirrors the decode of the generated DEMO label (demo_fixtures.render_label): DataMatrix + 8 Code 128 symbols.
GS, RS, EOT = "␝", "␞", "␄"


def label_codes(mouser_pn: str, cust_pn: str, *, po="40000001", line="018", qty="30",
                coo="TW", invoice="090000001", mfr="DEMO Corp", route="57DEMO") -> tuple[str, ...]:
    dm = (f"[)>{RS}06{GS}K{po}{GS}14K{line}{GS}P{cust_pn.replace(' ', chr(0x2420))}{GS}Q{qty}"
          f"{GS}11K{invoice}{GS}4L{coo}{GS}1V{mfr}{RS}{EOT}")
    return (dm, po, line, mouser_pn, cust_pn, qty, coo, invoice, route)


BAG_A = label_codes("901-DEMO-R2K2", "DEMO1 R4,5")
BAG_A_DESIG = ("R4", "R5")
# D7: the user shortened the Cust P/N for this 9-instance line.
BAG_B = label_codes("902-DEMO-C100N", "DEMO1 C2..", line="002", qty="100")
BAG_B_DESIG = ("C2", "C3", "C4", "C7", "C11", "C13", "C18", "C19", "C20")
BAG_UNKNOWN = label_codes("999-DEMO-NOTINBOM", "VIB R4", line="021")        # a part of another BOM
BAG_TUNING = label_codes("920-DEMO-R560", "DEMO1 R6 TRIM", line="031")
JUNK = ("40000001", "018", "TW")    # label half in view: no P/N decoded
