"""One connection to KiCad, one worker thread, a hard time budget on every call.

Rules from the requirements this file implements:
  KC-24  no socket file, no dial. The answer is built from the settings file and the file system alone.
  KC-27  a call returns its own result inside its budget; the budgets are below the server's outer bounds (5 / 8 / 12 s).
  KC-28  each request carries an nng send and receive timeout (TIMEOUT_MS, TBR-1).
  KC-29  all KiCad I/O of a process goes through this object's single worker thread, one request at a time.
  KC-31  a busy answer is retried BUSY_RETRIES times (TBR-2; 0), never in a loop that can hang.
  KC-32  kipy.check_version() is never called; a KiCad newer than the kipy build is a note.
  KC-35  only an ipc:// address is accepted.
Polybench never blocks: the caller waits on a Future with a timeout. If KiCad stalls, the worker thread is left to finish on its
own nng timeout, and jobs that were queued behind it are dropped when their deadline has passed (a late selection is never sent).
"""
from __future__ import annotations

import concurrent.futures as cf
import os
import queue
import threading
import time

from . import guard, settings
from .guard import ALLOWED, CommandRefused, GuardedClient

TIMEOUT_MS = int(os.environ.get("POLYBENCH_KICAD_TIMEOUT_MS", "2000"))      # TBR-1: kipy's default, not agreed
def parse_actions(spec: str | None) -> frozenset[str]:
    """View options from POLYBENCH_KICAD_ACTIONS: zoom, highlight, schematic (comma list), 'none' or '' for none. Default zoom,highlight."""
    spec = os.environ.get("POLYBENCH_KICAD_ACTIONS", "zoom,highlight") if spec is None else spec
    return frozenset(p.strip().lower() for p in spec.split(",") if p.strip().lower() in guard.ACTIONS)


BUSY_RETRIES = int(os.environ.get("POLYBENCH_KICAD_BUSY_RETRIES", "0"))     # TBR-2: 0, not agreed

# Time budgets (s), each below the server's outer bound for the same call.
BUDGET_STATUS = 4.0          # server: 5 s
BUDGET_SELECT = 6.0          # server: 8 s (Session.highlight, service.select_targets)
BUDGET_READ = 10.0           # server: 12 s (designators, documents)

INSTALL_HINT = "kicad-python not installed: pip install -e .[kicad]"


class LinkError(Exception):
    """KiCad could not be used. `state` is one of: no_kipy, api_off, not_running, stale_socket, timeout, busy, no_board,
    refused, bad_address, error."""

    def __init__(self, state: str, detail: str):
        super().__init__(detail)
        self.state, self.detail = state, detail

    @property
    def busy(self) -> bool:
        return self.state == "busy"


class GuardRefused(LinkError):
    """The expected-document guard (KC-09): the open board is not the one step 1 recorded. Nothing was sent."""

    def __init__(self, detail: str):
        super().__init__("guard", detail)


class _Expired(Exception):
    pass


class _Worker:
    """A daemon thread that runs jobs one at a time. A job whose deadline passed while it waited is dropped, not run."""

    def __init__(self, name="kicad-link"):
        self._q: queue.Queue = queue.Queue()
        self.thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self.thread.start()
        self.busy_since: float | None = None

    def submit(self, fn, deadline: float) -> cf.Future:
        fut: cf.Future = cf.Future()
        self._q.put((fn, deadline, fut))
        return fut

    def _loop(self):
        while True:
            fn, deadline, fut = self._q.get()
            if time.monotonic() > deadline:
                fut.set_exception(_Expired())
                continue
            self.busy_since = time.monotonic()
            try:
                fut.set_result(fn())
            except BaseException as e:      # noqa: BLE001 - handed to the caller
                fut.set_exception(e)
            finally:
                self.busy_since = None


class Ctx:
    """What a job sees: the kipy objects built on the guarded client, and the deadline."""

    def __init__(self, kicad, guard, deadline: float):
        self.kicad, self.guard, self.deadline = kicad, guard, deadline

    def check(self):
        if time.monotonic() > self.deadline:
            raise LinkError("timeout", "KiCad did not answer in time")

    def documents(self):
        from kipy.proto.common.types import DocumentType
        self.check()
        return list(self.kicad.get_open_documents(DocumentType.DOCTYPE_PCB))

    def board(self):
        """(Board, DocumentSpecifier) of the first open PCB, or LinkError('no_board')."""
        from kipy.board import Board
        docs = self.documents()
        if not docs:
            raise LinkError("no_board", "KiCad is running but no board is open in the PCB editor")
        return Board(self.guard, docs[0]), docs[0]


class KiCadLink:
    def __init__(self, client_factory=None, timeout_ms: int | None = None, busy_retries: int | None = None,
                 socket_path: str | None = None, fake: bool = False, actions: str | None = None):
        """client_factory(address) -> an object with send(command, response_type). Default: kipy's KiCadClient.
        fake=True: the factory is a stand-in for the server, there is no socket file to look for (tests, --simulate)."""
        self._factory = client_factory
        self.fake = fake
        self.timeout_ms = TIMEOUT_MS if timeout_ms is None else timeout_ms
        self.busy_retries = BUSY_RETRIES if busy_retries is None else busy_retries
        self.socket_path = socket_path
        self.actions = parse_actions(actions)       # view options left on
        self.highlight_on = False                   # we ran highlightNetSelection and have not cleared it
        self.log: list[str] = []              # every command type that went out (KC-33), across reconnects
        self.refused: list[str] = []
        self.dials = 0                        # connection attempts (KC-24: 0 when there is no socket)
        self.last_latency_ms: float | None = None
        self.expected_document: str | None = None
        self.owned_ids: list[str] = []        # KIIDs this process selected last (KC-20)
        self._worker = _Worker()
        self._guard: GuardedClient | None = None
        self._kicad = None
        self._address: str | None = None
        self._lock = threading.Lock()         # state around the worker, not around KiCad I/O

    # ------------------------------------------------------------------ pre-flight without a dial (KC-24, KC-25)
    def preflight(self) -> str:
        """The ipc:// address to dial, or LinkError with the reason. Never dials."""
        if self.fake:
            return "ipc://fake"
        try:
            import kipy  # noqa: F401
        except ImportError:
            raise LinkError("no_kipy", INSTALL_HINT) from None
        if self.socket_path:
            cands = [self.socket_path]
        else:
            cands = settings.socket_files()
        for c in cands:
            try:
                addr = settings.check_scheme(c)
            except ValueError as e:
                raise LinkError("bad_address", str(e)) from None
            if os.path.exists(addr[len("ipc://"):]):
                return addr
        enabled, _where = settings.api_enabled()
        if enabled is False:
            raise LinkError("api_off", settings.API_OFF_DETAIL)
        raise LinkError("not_running", settings.NOT_RUNNING_DETAIL)

    def socket_candidates(self) -> list[str]:
        return [self.socket_path] if self.socket_path else settings.socket_files()

    # ------------------------------------------------------------------ connection
    def _connect(self, address: str):
        if self._kicad is not None and address == self._address:
            return
        from kipy.kicad import KiCad
        self.dials += 1
        if self._factory is not None:
            inner = self._factory(address)
        else:
            from kipy.client import KiCadClient
            inner = KiCadClient(address, "polybench", "", self.timeout_ms)
        self._guard = GuardedClient(inner, actions={n for k in self.actions for n in guard.ACTIONS[k]})
        self._guard.log, self._guard.refused = self.log, self.refused         # one log for the life of the link
        self._kicad = KiCad.from_client(self._guard)
        self._address = address

    def _drop(self):
        self._kicad = self._guard = self._address = None

    def _map_exception(self, e: Exception) -> LinkError:
        if isinstance(e, LinkError):
            return e
        if isinstance(e, CommandRefused):
            return LinkError("refused", str(e))
        name = type(e).__name__
        msg = str(e)
        if name == "ApiError":
            code = getattr(e, "code", None)
            try:
                from kipy.proto.common import ApiStatusCode
                busy_codes = {ApiStatusCode.AS_BUSY, ApiStatusCode.AS_NOT_READY}
            except Exception:
                busy_codes = set()
            if code in busy_codes:
                return LinkError("busy", f"KiCad is busy ({msg}); finish or close the dialog or tool in the PCB editor")
            return LinkError("error", f"KiCad returned an error: {msg}")
        if name == "ConnectionError":
            low = msg.lower()
            self._drop()
            if "timed out" in low or "timeout" in low:
                return LinkError("timeout", f"KiCad did not answer ({msg})")
            return LinkError("stale_socket", f"KiCad's API socket does not answer ({msg})")
        return LinkError("error", f"{name}: {msg}")

    # ------------------------------------------------------------------ the one entry point
    def run(self, fn, budget: float, address: str | None = None):
        """Run fn(ctx) on the worker thread and wait at most `budget` seconds. Returns its result. Raises LinkError.
        fn does all its KiCad I/O through ctx; between requests it may call ctx.check()."""
        # no dial without a socket (KC-24): decided here, on the caller's thread, from the file system alone
        if address:
            address = settings.check_scheme(address)
            if not self.fake and not os.path.exists(address[len("ipc://"):]):
                raise LinkError("not_running", "the selected KiCad instance is no longer running (its API socket is gone)")
        else:
            address = self.preflight()
        deadline = time.monotonic() + budget

        def job():
            t0 = time.perf_counter()
            attempts = 0
            while True:
                try:
                    self._connect(address)
                    ctx = Ctx(self._kicad, self._guard, deadline)
                    return fn(ctx)
                except Exception as e:      # noqa: BLE001
                    err = self._map_exception(e)
                    attempts += 1
                    if err.busy and attempts <= self.busy_retries and time.monotonic() + 0.3 < deadline:
                        time.sleep(0.2)
                        continue
                    raise err from None
                finally:
                    self.last_latency_ms = (time.perf_counter() - t0) * 1000

        fut = self._worker.submit(job, deadline)
        try:
            return fut.result(timeout=budget)
        except cf.TimeoutError:
            raise LinkError("timeout", f"KiCad did not answer within {budget:.0f} s") from None
        except _Expired:
            raise LinkError("timeout", "KiCad is still busy with an earlier request") from None

    # ------------------------------------------------------------------ small conveniences used by several callers
    def version_note(self, ctx: Ctx) -> tuple[str, str]:
        """(full version string, note). The note is non-empty when KiCad is newer than the kipy build (KC-32)."""
        v = ctx.kicad.get_version()
        full = str(getattr(v, "full_version", "") or f"{v.major}.{v.minor}.{v.patch}")
        note = ""
        try:
            api = ctx.kicad.get_api_version()
            if v > api:
                note = (f"KiCad {v.major}.{v.minor}.{v.patch} is newer than the kicad-python build "
                        f"({api.major}.{api.minor}.{api.patch}); working, not checked against the newer API")
        except Exception:
            pass
        return full, note


_shared: KiCadLink | None = None
_shared_lock = threading.Lock()


def shared_link() -> KiCadLink:
    """The process's one link (KC-29): the bridge and the scanner client both use it."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = KiCadLink()
        return _shared


def reset_shared(link: KiCadLink | None = None) -> None:
    """Tests: replace (or drop) the process-wide link."""
    global _shared
    with _shared_lock:
        _shared = link
