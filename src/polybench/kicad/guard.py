"""The command allow-list (KC-33) and the command log, in front of every request that leaves for KiCad.

`GuardedClient` wraps a kipy `KiCadClient` (or the fake): kipy's `KiCad` and `Board` objects are built on top of it, so every
command passes `send()`. A command whose message class is not on the list is refused here, before it is serialised: nothing
reaches the socket. The log of command types is what the acceptance run reports.
"""
from __future__ import annotations

import os
import threading

# Read commands, then the three selection commands. Nothing that saves, reverts, commits, creates, updates or deletes items,
# nothing that runs a tool action or moves items.
ALLOWED = frozenset({
    "GetVersion", "Ping", "GetOpenDocuments",
    "GetItems", "GetItemsById", "GetItemsByNet",
    "GetNets", "GetSelection",
    "AddToSelection", "RemoveFromSelection", "ClearSelection",
})
# View actions through RunAction (KiCad calls that API unstable, so it is opt-out and fenced): only these five names, only for the
# options the operator left on, and none of them edits anything (measured live, 17 Oct 2026: status RAS_OK, files unchanged).
ACTIONS = {
    "zoom": ("common.Control.zoomFitSelection", "common.Control.centerSelection"),
    "highlight": ("pcbnew.EditorControl.highlightNetSelection", "pcbnew.EditorControl.clearHighlight"),
    "schematic": ("pcbnew.InteractiveSelection.SelectOnSchematic",),
}
ALL_ACTION_NAMES = frozenset(n for v in ACTIONS.values() for n in v)
SEEN_ACTIONS: set[str] = set()
SEEN: set[str] = set()          # every command type that passed a guard in this process (the test suite asserts on it)
SELECTION_WRITES = frozenset({"AddToSelection", "RemoveFromSelection", "ClearSelection"})


class CommandRefused(Exception):
    """A request that is not on the allow-list. Raised before anything is sent."""


def _journal(entry: str) -> None:
    """Env POLYBENCH_KICAD_COMMAND_LOG=<file>: append every command type that passes (the acceptance run reads it). Off by default."""
    path = os.environ.get("POLYBENCH_KICAD_COMMAND_LOG")
    if path:
        try:
            with open(path, "a") as fh:
                fh.write(entry + "\n")
        except OSError:
            pass


class GuardedClient:
    def __init__(self, inner, allowed=ALLOWED, actions=frozenset()):
        self._inner = inner
        self._allowed = frozenset(allowed)
        self._actions = frozenset(actions) & ALL_ACTION_NAMES      # RunAction names this client may send; empty: RunAction is refused
        self.log: list[str] = []
        self.refused: list[str] = []
        self._lock = threading.Lock()

    def send(self, command, response_type):
        name = type(command).__name__
        if name == "RunAction":
            act = getattr(command, "action", "")
            if act not in self._actions:
                with self._lock:
                    self.refused.append(f"RunAction:{act}")
                raise CommandRefused(f"RunAction {act!r} is not an enabled Polybench view action; nothing was sent")
            SEEN_ACTIONS.add(act)
            _journal(f"RunAction:{act}")
        elif name not in self._allowed:
            with self._lock:
                self.refused.append(name)
            raise CommandRefused(f"command {name} is not on the Polybench allow-list; nothing was sent")
        with self._lock:
            self.log.append(name)
            SEEN.add(name)
        if name != "RunAction":
            _journal(name)
        return self._inner.send(command, response_type)

    def __getattr__(self, item):          # connected, close, ... of the inner client
        return getattr(self._inner, item)
