"""Stability rule (R7, TBR values): the last N consecutive valid readings lie within +-pct of their mean, with a floor of +-1 count of
the last displayed digit. A change of mode or range resets the window; so does anything that is not a number."""
from __future__ import annotations

from collections import deque
from decimal import Decimal

N_DEFAULT = 5
PCT_DEFAULT = Decimal("0.2")


class StabilityWindow:
    def __init__(self, n: int = N_DEFAULT, pct: Decimal | float | str = PCT_DEFAULT):
        self.n = max(2, int(n))
        self.pct = Decimal(str(pct))
        self.vals: deque[Decimal] = deque(maxlen=self.n)
        self.ctx = None

    def reset(self):
        self.vals.clear()
        self.ctx = None

    def push(self, value: Decimal, context, count: Decimal) -> bool:
        """Add a valid reading; True when the window is now stable."""
        if context != self.ctx:
            self.vals.clear()
            self.ctx = context
        self.vals.append(value)
        return self.stable(count)

    def mean(self) -> Decimal | None:
        return sum(self.vals) / len(self.vals) if self.vals else None

    def tolerance(self, count: Decimal) -> Decimal:
        m = self.mean() or Decimal(0)
        return max(abs(m) * self.pct / 100, count)

    def stable(self, count: Decimal) -> bool:
        if len(self.vals) < self.n:
            return False
        m = self.mean()
        tol = self.tolerance(count)
        return all(abs(v - m) <= tol for v in self.vals)

    def progress(self) -> tuple[int, int]:
        return len(self.vals), self.n
