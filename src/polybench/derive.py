"""Tests answered by data an action already collected (PW-6). An action may declare

    derive:
      - {test: P06, from: analysis.within_limit, map: {true: "yes", false: "no"}, note: "..."}

The plugin reports an analysis block (`ctx.analysis(...)`: min, max, mean, final, tripped, settled, ...). Each rule reads one dotted path
of it and proposes the mapped value for the test, through the same path as any instrument reading (so the limit, the stop rule and the
history work unchanged). A path with no value derives nothing and the test stays a question for the operator. Pure functions; no verdicts.
"""
from __future__ import annotations

import json
from typing import Any

_MISSING = object()


def dig(analysis: dict, path: str) -> Any:
    """analysis['a']['b'] for 'analysis.a.b' or 'a.b'; _MISSING if absent or None."""
    p = str(path or "").strip()
    if p.startswith("analysis."):
        p = p[len("analysis."):]
    cur: Any = analysis
    for k in p.split("."):
        if isinstance(cur, dict) and k in cur and cur[k] is not None:
            cur = cur[k]
        else:
            return _MISSING
    return cur


def _key(v: Any) -> str:
    return str(v).lower() if isinstance(v, bool) else str(v)


def normalise(rules: Any) -> list[dict]:
    """The rules of an action as a clean list; entries that are not mappings with `test` and `from` are dropped (lint reports them)."""
    return [r for r in (rules if isinstance(rules, list) else []) if isinstance(r, dict) and r.get("test") and r.get("from")]


def derive_values(rules: list[dict], analysis: dict) -> tuple[list[dict], list[str]]:
    """-> (derived, skipped). derived rows: {test, value, unit, source_path, source_value, note}; skipped: plain-English reasons."""
    derived, skipped = [], []
    for r in normalise(rules):
        v = dig(analysis, r["from"])
        if v is _MISSING:
            skipped.append(f"{r['test']}: analysis has no {r['from']}; left for the operator")
            continue
        mp = r.get("map")
        if isinstance(mp, dict):
            m = {_key(k): x for k, x in mp.items()}
            if _key(v) not in m:
                skipped.append(f"{r['test']}: {r['from']} = {v!r} is not in the map; left for the operator")
                continue
            val = str(m[_key(v)])
        elif isinstance(v, bool):
            val = "yes" if v else "no"
        elif isinstance(v, float):
            val = str(float(f"{v:.6g}"))
        else:
            val = str(v)
        derived.append({"test": str(r["test"]), "value": val, "unit": r.get("unit"), "source_path": str(r["from"]),
                        "source_value": v if isinstance(v, (bool, int, float, str)) else json.dumps(v), "note": str(r.get("note") or "")})
    return derived, skipped
