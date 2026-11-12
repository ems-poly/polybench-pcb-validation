# Ported from the earlier voice prototype prg/rowmode.py. Changes for Polybench: none.
"""Batch (row) testing: one test across all boards, then the next test.

The technician measures C04 on B1..B5, then C05 on B1..B5. In row mode the cursor is a row and the
current board is "the next board that still needs a value". Pure functions here; Session owns the state.
"""
from __future__ import annotations

import re

from . import grammar


def next_slot(order: list[str], settled, start: str | None = None, exclude=(), inclusive: bool = False) -> str | None:
    """The next board to fill in `order` (B1..B5).

    Starts after `start` (or at `start` itself when inclusive), takes the first board that is not settled
    (filled, pending, accepted or skipped) and not in `exclude`, then wraps to the beginning of the row.
    None when every board is settled.
    """
    if not order:
        return None
    i0 = order.index(start) if start in order else -1
    seq = order[i0 + (0 if inclusive else 1):] + order[:max(i0 + (0 if inclusive else 1), 0)]
    if start not in order:
        seq = list(order)
    for b in seq:
        if b not in exclude and not settled(b):
            return b
    return None


def split_pairs(text: str, idx) -> list[str] | None:
    """'C04 B1 3.29, B2 3.30, B3 3.31' -> ['C04 B1 3.29', 'C04 B2 3.30', 'C04 B3 3.31'], or None.

    Only a sequence of board-labelled values is split (two or more boards, each followed by its value).
    An ID or other words before the first board are prefixed to every segment. Anything else is left to the
    single-value grammar or the LLM.
    """
    toks, orig = grammar._tokens(text)
    starts: list[int] = []
    i = 0
    while i < len(toks):
        b = grammar._board_at(toks, i)
        if b is not None and b[0] != "all":
            if len(b[0]) != 1:
                return None                      # "boards 1 and 2 3.3" is one value for two boards: not a list
            starts.append(i)
            i = b[1]
            continue
        i += 1
    if len(starts) < 2:
        return None
    prefix = " ".join(orig[:starts[0]]).strip()
    segs = []
    for a, b in zip(starts, starts[1:] + [len(toks)]):
        segs.append(((prefix + " ") if prefix else "") + " ".join(orig[a:b]))
    return segs


# --- board skipping: "skip B3, reworking" -------------------------------------------------------
def parse_skip(text: str):
    """(boards, reason) for 'skip B3 reworking' / 'skip boards 3 and 4, not fitted'; None if it is not a skip with a board."""
    toks, orig = grammar._tokens(text)
    if not toks or toks[0] not in grammar.SKIP:
        return None
    boards: list[int] = []
    i = 1
    n = len(toks)
    while i < n:
        b = grammar._board_at(toks, i)
        if b is None:
            break
        if b[0] == "all":
            return None
        boards += [x for x in b[0] if x not in boards]
        i = b[1]
        if i < n and toks[i] == "and":
            i += 1
    if not boards:
        return None
    reason = " ".join(orig[i:]).strip(" ,.;:")
    reason = re.sub(r"^(because|since|as|for)\s+", "", reason, flags=re.I)
    return boards, reason
