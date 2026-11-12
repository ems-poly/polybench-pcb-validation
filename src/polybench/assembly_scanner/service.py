"""Headless Assembly Scanner: the PoC's window logic (app/ui.py) without Qt, for the Polybench server.

What is ported here from ui.py, unchanged in rule and almost unchanged in code:
  ScanWorker      decode loop on a daemon thread, generation numbers, stall watchdog, pause, mirror
  FusionWorker    one thread for every Fusion call, pause, recursion circuit breaker
  poll / auto     board list poll every 15 s, back-off 15/30/60 s, circuit open on a recursion error, auto board pick by
                  placement coverage (signature of BOM + open boards), manual pick, rescan, closed-board handling
  board_ctx / hud_ctx / HUD sections, camera open with permission handling, pause when idle (camera released)
What changed: Qt signals became callbacks, Qt timers became one housekeeping thread, painting became state() for the page,
the application-focus pause became "the Assembly page is not open" (heartbeat), and the BOM comes from the batch protocol
through the PoC's own importer instead of a file picker.

Thread model: everything that touches state takes self._lock (RLock). Callbacks (on_fire, on_change) are called from worker
threads; the server marshals them onto its event loop.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from polybench.extras import lazy as _lazy
cv2 = _lazy("cv2", "scanner")

from polybench.assembly_scanner import scanner
from polybench.assembly_scanner.camera import (DENIED_HELP, WAITING_HELP, Camera, auth_status, list_cameras,
                                               request_access)
from polybench.assembly_scanner.hud import hud_sections
from polybench.assembly_scanner.main import Controller
from polybench.assembly_scanner.models import Bom, HighlightResult, Resolution, Status


def bom_placeable_designators(bom: Bom) -> list[str]:
    seen: list[str] = []
    for ln in bom.lines:
        if ln.fitted:
            for d in ln.designators:
                if d not in seen:
                    seen.append(d)
    return seen


def bom_summary(bom: Bom) -> str:
    """'[Basket, Tuning kit] · 24 placements · 16 extras' (file name not included)."""
    rep = getattr(bom, "report", None)
    if rep is not None:
        sheets = [s.sheet for s in rep.sheets if getattr(s, "lines", 0)]
        return f"[{', '.join(sheets)}] · {rep.placement_pns} placements · {rep.extra_pns} extras"
    sheets = sorted({ln.sheet for ln in bom.lines})
    pns = bom.by_mouser
    placed = sum(1 for ls in pns.values() if any(l.fitted and l.designators for l in ls))
    return f"[{', '.join(sheets)}] · {placed} placements · {len(pns) - placed} extras"


# --- background workers ------------------------------------------------------------------------------
class ScanWorker:
    """Decode loop with a stall watchdog (ui.ScanWorker). Callbacks: on_live(dict), on_event(Event), on_stalled(dict)."""
    STALL_S = 1.5

    def __init__(self, controller: Controller, on_live, on_event, on_stalled):
        self.ctl = controller
        self.on_live, self.on_event, self.on_stalled = on_live, on_event, on_stalled
        self.camera: Camera | None = None
        self._running = True
        self._paused = True            # idle until the service resumes it
        self._gen = 0
        self._lock = threading.Lock()
        self._busy_since: float | None = None
        self.stall_count = 0
        self._thread: threading.Thread | None = None
        self.flip_camera = False

    def start(self) -> None:
        self._spawn()

    def _spawn(self) -> None:
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._busy_since = None
        self._thread = threading.Thread(target=self._loop, args=(gen,), name=f"scan-{gen}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        with self._lock:
            self._gen += 1
        if self._thread is not None:
            self._thread.join(1.0)

    def set_paused(self, paused: bool) -> None:
        """Invalidate an in-flight decode while idle."""
        with self._lock:
            if self._paused == paused:
                return
            self._paused = paused
            self._gen += 1
            gen = self._gen
            self._busy_since = None
        if not paused and self._running:
            self._thread = threading.Thread(target=self._loop, args=(gen,), name=f"scan-{gen}", daemon=True)
            self._thread.start()

    def check_stall(self) -> None:
        with self._lock:
            since = self._busy_since
        if since is None:
            return
        age = time.monotonic() - since
        if age > self.STALL_S:
            self.stall_count += 1
            self._spawn()           # abandon the stuck generation
            self.on_stalled({"count": self.stall_count, "ms": age * 1000})

    def _current(self, gen: int) -> bool:
        return self._running and not self._paused and gen == self._gen

    def _loop(self, gen: int) -> None:
        last_seq, last_emit, hist = -1, 0.0, []
        while self._current(gen):
            cam = self.camera
            seq, frame = cam.latest() if cam is not None else (last_seq, None)
            if frame is None or seq == last_seq:
                time.sleep(0.005)
                continue
            last_seq = seq
            t0 = time.perf_counter()
            with self._lock:
                if gen != self._gen:
                    return
                self._busy_since = time.monotonic()
            try:
                scan_frame = cv2.flip(frame, 1) if self.flip_camera else frame
                codes = self.ctl.decode_frame(scan_frame)
                err = None
            except Exception as e:  # never kill the loop; show it
                codes, err = [], f"decode error: {type(e).__name__}: {e}"
            with self._lock:
                if gen != self._gen:
                    return            # abandoned while decoding: drop the result
                self._busy_since = None
            if err:
                self.on_live({"error": err, "t": time.monotonic()})
                time.sleep(0.2)
                continue
            ms = (time.perf_counter() - t0) * 1000
            ev = self.ctl.step_codes(codes, time.monotonic(), run_fusion=False)
            ev.decode_ms = ms
            if ev.fired is not None:
                self.on_event(ev)
            hist = (hist + [ms])[-30:]
            now = time.monotonic()
            if now - last_emit > 0.2:
                last_emit = now
                st = ev.resolution.status if ev.resolution is not None else None
                self.on_live({"codes": ev.codes, "ms": sum(hist) / len(hist), "t": now, "status": st})


class FusionWorker:
    """Runs FusionClient calls on one background thread (ui.FusionWorker). Callbacks run on that thread."""
    _PAUSED_FMT = "{0} request skipped while Assembly Scanner is paused"
    _CIRCUIT_FMT = "{0} requests stopped after a recursion error; use Rescan boards to retry once {0} is ready"
    PAUSED_ERROR = _PAUSED_FMT.format("Fusion")
    CIRCUIT_ERROR = _CIRCUIT_FMT.format("Fusion")

    @property
    def paused_error(self) -> str:
        return self._PAUSED_FMT.format(self._program())

    @property
    def circuit_error(self) -> str:
        return self._CIRCUIT_FMT.format(self._program())

    def __init__(self, deliver: Callable | None = None, program_fn: Callable[[], str] | None = None):
        self._program = program_fn or (lambda: "Fusion")     # operator-facing ECAD program name
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fusion")
        self._deliver = deliver or (lambda cb, r, e: cb(r, e))
        self.pending = 0
        self._paused = False
        self._recursion_blocked = False
        self._state_lock = threading.Lock()

    def set_paused(self, paused: bool) -> None:
        with self._state_lock:
            self._paused = paused

    def reset_recursion_block(self) -> None:
        """Allow one explicit retry after the user has reset/recovered Fusion."""
        with self._state_lock:
            self._recursion_blocked = False

    def submit(self, fn, cb, force: bool = False) -> None:
        """force: an explicit operator action (typed scan, picked line) is sent even while the scanner is paused."""
        with self._state_lock:
            self.pending += 1

        def job():
            with self._state_lock:
                paused = self._paused and not force
                recursion_blocked = self._recursion_blocked
            if paused:
                r, e = None, self.paused_error
            elif recursion_blocked:
                r, e = None, self.circuit_error
            else:
                try:
                    r, e = fn(), None
                    if "maximum recursion depth" in str(r).lower():
                        with self._state_lock:
                            self._recursion_blocked = True
                except Exception as ex:  # surfaced in the HUD
                    r, e = None, f"{type(ex).__name__}: {ex}"
                    if isinstance(ex, RecursionError) or "maximum recursion depth" in str(ex).lower():
                        with self._state_lock:
                            self._recursion_blocked = True
            with self._state_lock:
                self.pending -= 1
            self._deliver(cb, r, e)
        self._pool.submit(job)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


# --- the service ---------------------------------------------------------------------------------------
class AssemblyService:
    program: str = "Fusion"    # operator-facing ECAD program name; polybench_link.build_service sets it
    POLL_MS = 15000
    HEARTBEAT_S = 6.0          # the page polls every ~0.5 s while the Assembly step is open

    def __init__(self, settings, fusion, camera_factory=Camera, on_fire: Callable | None = None,
                 on_change: Callable | None = None, camera_list: Callable | None = None, autostart: bool = True):
        self.settings = settings
        self.fusion = fusion
        self.camera_factory = camera_factory
        self.on_fire, self.on_change = on_fire, on_change
        self._camera_list = camera_list or list_cameras
        self._lock = threading.RLock()
        self.ctl = Controller(fusion=fusion)
        self.fworker = FusionWorker(deliver=self._deliver, program_fn=lambda: self.program)
        self.worker = ScanWorker(self.ctl, self._on_live, self._on_event, self._on_stalled)

        # operator intent
        self.enabled = autostart          # Start / Stop
        self._manual_paused = False       # Pause / Resume
        self._page_active = False         # the Assembly page is open (heartbeat)
        self._background = False          # the page reported itself hidden (Page Visibility): paused at once, heartbeats ignored
        self.ecad_off: str | None = None  # step 1 'Continue without ECAD': no selection command goes to Fusion
        self._hb = 0.0
        self._idle = True
        self._explicit_until = 0.0

        self.bom: Bom | None = None
        self.bom_error: str | None = None
        self.bom_name = ""
        self.line_map: dict[int, tuple] = {}     # Polybench BOM line -> PoC BomLines
        self.pn_lines: dict[str, list[int]] = {} # normalised Mouser P/N -> Polybench BOM lines
        self.fusion_status: dict = {}
        self.boards: list[dict] = []
        self._poll_in_flight = False
        self._poll_failures = 0
        self._poll_retry_at = 0.0
        self._last_poll_error: str | None = None
        self._same_poll_errors = 0
        self._poll_circuit_open = False
        self.auto_info = None
        self.auto_err: str | None = None
        self.pick_err: str | None = None
        self._pinned_key = None
        self.board_pick = None
        self.live: dict = {}
        self.camera: Camera | None = None
        self.settings_err: str | None = None
        self._last_auto_sig = None
        self._pending_hl: Resolution | None = None
        self._blocked: str | None = None
        self._rescan_pending = False
        self.stall_info: dict | None = None
        self._label_seen_t = 0.0
        self._asked_permission = False
        self.fire_seq = 0
        self.fired_info: dict = {"seq": 0}
        self._closed = False
        self.worker.flip_camera = bool(getattr(settings, "camera_inverted", False))
        self.worker.start()
        self._thread = threading.Thread(target=self._housekeeping, name="scanner-housekeeping", daemon=True)
        self._thread.start()
        self.open_camera(int(settings.camera_index))
        self._apply_idle_state()

    # ------------------------------------------------------------ plumbing
    def _deliver(self, cb, r, e) -> None:
        with self._lock:
            cb(r, e)
        self._changed()

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def _housekeeping(self) -> None:
        """ui timers: stall watchdog (250 ms), Fusion poll (15 s), plus the page heartbeat."""
        last_poll = 0.0
        while not self._closed:
            time.sleep(0.25)
            try:
                with self._lock:
                    self._check_heartbeat()
                    if not self._idle:
                        self.worker.check_stall()
                        if time.monotonic() - last_poll >= self.POLL_MS / 1000:
                            last_poll = time.monotonic()
                            self.poll_fusion()
            except Exception:
                pass

    def _check_heartbeat(self) -> None:
        if self._page_active and time.monotonic() - self._hb > self.HEARTBEAT_S:
            self._page_active = False
            self._apply_idle_state()

    def set_background(self, hidden: bool) -> None:
        """Page Visibility from the page: hidden/minimised pauses capture and decoding at once; visible resumes. A visible
        but unfocused window (half screen beside Fusion) is visible and keeps scanning."""
        with self._lock:
            self._background = bool(hidden)
            if hidden:
                if self._page_active:
                    self._page_active = False
                    self._apply_idle_state()
        self._changed()

    def touch(self) -> None:
        """The Assembly page asked for the state: it is open and in front."""
        with self._lock:
            if self._background:
                return                   # a late heartbeat from a page that has since gone to the background
            self._hb = time.monotonic()
            if not self._page_active:
                self._page_active = True
                self._apply_idle_state()

    def leave(self) -> None:
        """The operator left the Assembly step: release the camera now."""
        with self._lock:
            if self._page_active:
                self._page_active = False
                self._apply_idle_state()

    def shutdown(self) -> None:
        self._closed = True
        self.worker.stop()
        if self.camera:
            self.camera.stop()
        self.fworker.shutdown()

    # ------------------------------------------------------------ pause / start / stop (ui._apply_idle_state)
    def _is_idle(self) -> bool:
        return (not self.enabled) or self._manual_paused or (not self._page_active)

    def _apply_idle_state(self) -> None:
        """Stop camera capture and decoding while stopped, paused or not on the Assembly step."""
        paused = self._is_idle()
        was = self._idle
        self._idle = paused
        self.fworker.set_paused(paused)
        self.worker.set_paused(paused)
        if paused:
            if self.camera:
                self.camera.stop()
        else:
            if self.camera and self.camera.state in ("stopped", "stopping"):
                self.camera.start()
            if was:
                if self._rescan_pending:
                    self.rescan_boards()
                else:
                    self.poll_fusion()

    def _explicit(self) -> bool:
        return time.monotonic() < self._explicit_until

    def _fusion_idle(self) -> bool:
        """Idle for Fusion purposes: stopped, paused or page closed, unless the operator just asked for something."""
        return bool(self.ecad_off) or (self._is_idle() and not self._explicit())

    @property
    def pending_reasons(self) -> tuple:
        return (f"checking {self.program}…", "choosing…")

    def ensure_board(self, timeout: float = 5.0) -> None:
        """An explicit operator action (typed scan, picked line) needs a board decision even while the camera is paused:
        poll once if Fusion was never asked, then wait for the auto pick."""
        if self.ecad_off:
            return
        deadline = time.monotonic() + timeout
        with self._lock:
            self._explicit_until = deadline + 2
        while time.monotonic() < deadline:
            with self._lock:
                ctx = self.board_ctx()
                if ctx["_ok"] or (self.fusion_status and ctx["_reason"] not in self.pending_reasons):
                    return
                if not self._poll_in_flight and not self.fusion_status:
                    self.poll_fusion(force=True)
            time.sleep(0.05)

    def set_enabled(self, on: bool) -> None:
        with self._lock:
            self.enabled = bool(on)
            self._apply_idle_state()
        self._changed()

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self._manual_paused = bool(paused)
            self._apply_idle_state()
        self._changed()

    def set_mirror(self, on: bool) -> None:
        with self._lock:
            self.settings.camera_inverted = bool(on)
            self.worker.flip_camera = bool(on)
            self.settings_err = self.settings.save()
        self._changed()

    # ------------------------------------------------------------ camera (ui.open_camera)
    def cameras(self) -> list[dict]:
        names = {c.index: c.name for c in self._camera_list()}
        idxs = sorted(set(names) | {0, 1, 2} | {int(self.settings.camera_index)})
        return [{"index": i, "name": names.get(i, "camera")} for i in idxs]

    def open_camera(self, index: int) -> None:
        """Start capture only once camera access is authorised (the permission prompt is requested here)."""
        with self._lock:
            if self.camera is not None:
                self.camera.stop()
            self.camera = self.camera_factory(int(index))
            self.worker.camera = self.camera
            if int(self.settings.camera_index) != int(index):
                self.settings.camera_index = int(index)
                self.settings_err = self.settings.save()
            st = auth_status() if self.camera_factory is Camera else "authorized"
            if st == "not determined":
                self.camera.state, self.camera.error = "waiting-permission", WAITING_HELP
                if not self._asked_permission:
                    self._asked_permission = True
                    if not request_access(self._on_cam_access):
                        self.camera.state, self.camera.error = "error", "AVFoundation (pyobjc) not available"
                return
            if st in ("denied", "restricted"):
                self.camera.state, self.camera.error = "error", DENIED_HELP
                return
            if self._is_idle():
                self.camera.state = "stopped"
                return
            self.camera.start()
        self._changed()

    def _on_cam_access(self, granted: bool) -> None:
        with self._lock:
            if self.camera is None:
                return
            if granted and not self._is_idle():
                self.camera.start()
            elif granted:
                self.camera.state = "stopped"
            else:
                self.camera.state, self.camera.error = "error", DENIED_HELP
        self._changed()

    def jpeg(self, max_w: int = 960, quality: int = 72) -> tuple[int, bytes | None]:
        """Newest camera frame as JPEG (mirrored like the scan), with its sequence number."""
        cam = self.camera
        if cam is None or cam.state != "running":
            return -1, None
        seq, frame = cam.latest()
        if frame is None:
            return -1, None
        if self.worker.flip_camera:
            frame = cv2.flip(frame, 1)
        h, w = frame.shape[:2]
        if w > max_w:
            frame = cv2.resize(frame, (max_w, int(h * max_w / w)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return seq, (buf.tobytes() if ok else None)

    # ------------------------------------------------------------ BOM
    def set_bom(self, bom: Bom, name: str = "", line_map=None, pn_lines=None) -> None:
        with self._lock:
            self.bom, self.bom_error = bom, None
            self.bom_name = name or Path(bom.source_path).name
            self.line_map, self.pn_lines = dict(line_map or {}), dict(pn_lines or {})
            self.ctl.set_bom(bom)
            self._last_auto_sig = None
            if self.fusion.mode == "auto":
                self.poll_fusion(force=True)
        self._changed()

    def apply_project(self, info: dict | None, load_bom) -> bool:
        """Step 1 Project, when done, decides the BOM and the board; the PoC's own choices are the fallback.
        info: {ecad_connected, document_name, document_id, board, bom_source, bom_lines, crosscheck}. `load_bom(lines)` turns
        Polybench BOM lines into (Bom, line_map, pn_lines). Returns True when something changed."""
        if not info:
            return False
        sig = (info.get("document_id"), info.get("document_name"), len(info.get("bom_lines") or ()), info.get("bom_source"))
        with self._lock:
            if sig == getattr(self, "_project_sig", None):
                return False
            self._project_sig = sig
        lines = info.get("bom_lines")
        if lines:
            try:
                bom, lmap, pmap = load_bom(lines)
                self.set_bom(bom, f"project BOM ({info.get('bom_source') or 'step 1'})", lmap, pmap)
            except Exception as e:
                self.set_bom_error(f"cannot load the project BOM into the scanner: {e}")
        key = info.get("document_id") or info.get("document_name")
        if key and info.get("ecad_connected", True):
            self._explicit_until = time.monotonic() + 8     # the pick goes out even before the page's first heartbeat
            self.pick_board(key)         # a board chosen in step 1 is a manual pin; otherwise the coverage-based auto pick stays
        return True

    def set_bom_error(self, msg: str) -> None:
        with self._lock:
            self.bom_error = msg
        self._changed()

    # ------------------------------------------------------------ Fusion: board choice (ui.poll_fusion ... run_auto_select)
    def poll_fusion(self, force: bool = False, reset_recursion: bool = False) -> None:
        with self._lock:
            if self._fusion_idle():
                return
            if reset_recursion:
                self.fworker.reset_recursion_block()
                self._poll_circuit_open = False
                self._same_poll_errors = 0
                self._last_poll_error = None
                self._poll_failures = 0
                self._poll_retry_at = 0.0
            elif force:
                self._poll_retry_at = 0.0
            if self._poll_in_flight:
                return
            if self._poll_circuit_open or (not force and time.monotonic() < self._poll_retry_at):
                return
            client = self.fusion
            self._poll_in_flight = True

        def job():
            st = client.status()
            boards = st.get("open_boards") if st.get("connected") else None
            if st.get("connected") and boards is None:
                try:
                    boards = client.list_boards()
                except Exception as e:
                    st = st | {"connected": False, "error": str(e)}
            return st, boards
        self.fworker.submit(job, self._on_poll, force=self._explicit())

    def _on_poll(self, r, err) -> None:
        self._poll_in_flight = False
        if self._fusion_idle() and err == self.fworker.paused_error:
            return
        st, boards = ({"connected": False, "error": err}, None) if err else r
        self.fusion_status = st
        if st.get("connected") and boards is not None:
            self._poll_failures = 0
            self._poll_retry_at = 0.0
            self._last_poll_error = None
            self._same_poll_errors = 0
        else:
            self._poll_failures += 1
            message = str(st.get("error") or f"{self.program} returned no board list")
            if message == self._last_poll_error:
                self._same_poll_errors += 1
            else:
                self._last_poll_error, self._same_poll_errors = message, 1
            if "RecursionError" in message or "maximum recursion depth" in message.lower():
                self._poll_circuit_open = True
                self._poll_retry_at = float("inf")
            else:
                delay = min(60.0, 15.0 * (2 ** min(self._poll_failures - 1, 2)))
                self._poll_retry_at = time.monotonic() + delay
        if boards is not None:
            self.boards = [{"key": b.get("key"), "name": str(b.get("ui_name", b.get("name", "?")))} for b in boards]
            if self.fusion.mode == "auto" and self.bom is not None:
                sig = (id(self.bom), tuple(sorted(str(b.get("key", "")) for b in boards)))
                if sig != self._last_auto_sig:
                    self._last_auto_sig = sig
                    self.run_auto_select(boards=boards)
        self.update_board_gate()
        if self._rescan_pending and not self._fusion_idle():
            self._rescan_pending = False
            self.rescan_boards()

    def rescan_boards(self) -> None:
        """Re-list boards; in auto mode also re-run the BOM coverage pick."""
        with self._lock:
            if self._is_idle():
                self.pick_err = f"Resume Assembly Scanner before rescanning; paused mode sends no {self.program} requests"
                self.update_board_gate()
                return
            self._last_auto_sig = None
            if self._poll_in_flight:
                self._rescan_pending = True
                self.pick_err = f"Rescan queued; waiting for the current {self.program} request to finish"
                self.update_board_gate()
                return
            self.pick_err = None
            self.poll_fusion(force=True, reset_recursion=True)

    def pick_board(self, key) -> None:
        """key None = Auto."""
        with self._lock:
            if self._fusion_idle():
                return
            client = self.fusion
            self._pinned_key = key
            self.fworker.submit(lambda: client.select_board(key), lambda r, e: self._after_pick(e, auto=key is None), force=self._explicit())

    def _after_pick(self, err, auto=False) -> None:
        if self._fusion_idle() and err == self.fworker.paused_error:
            return
        self.pick_err = f"cannot select board: {err}" if err else None
        if err:
            self._pinned_key = None
        if auto and not err and self.bom is not None:
            self.run_auto_select()
        else:
            self.poll_fusion()
        self.update_board_gate()

    def run_auto_select(self, boards: list[dict] | None = None) -> None:
        if self.bom is None or self._fusion_idle():
            return
        desigs = set(bom_placeable_designators(self.bom))
        client = self.fusion
        self.fworker.submit(lambda: client.auto_select_for(desigs, boards=boards), self._on_auto, force=self._explicit())

    def _on_auto(self, r, err) -> None:
        if self._fusion_idle() and err == self.fworker.paused_error:
            return
        self.auto_info, self.auto_err = (None, err) if err else (r, None)
        self.update_board_gate()

    def board_ctx(self) -> dict:
        """Board part of the HUD ctx, and whether highlighting is allowed (UI-3)."""
        program = getattr(self, "program", "Fusion")
        st, client = self.fusion_status, self.fusion
        mode, tgt = client.mode, client.target
        ctx = {"board_mode": mode, "board_name": (tgt or {}).get("name"), "program": program}
        if self.pick_err:
            ctx["fusion_error"] = self.pick_err
        elif not st:
            ctx["board_warning"] = f"checking {program}…"
        elif not st.get("connected"):
            reason = f"not connected: {st.get('error') or 'no reply'}"
            if self._poll_circuit_open:
                reason += "; automatic retries paused after repeated recursion errors — click Rescan boards"
            ctx["fusion_error"] = reason
        elif mode == "manual" and st.get("error"):
            ctx["fusion_error"] = f"{st['error']}. Pick another board or Auto."
        elif tgt is None:
            if self.bom is None:
                ctx["board_warning"] = "load a BOM to auto-pick"
            elif self.auto_err:
                ctx["board_warning"] = self.auto_err
            elif self.auto_info and self.auto_info[0]:
                best = self.auto_info[0]
                ctx["board_warning"] = (f"best is {best.get('name')} "
                                        f"({best.get('coverage', 0):.0%}): {best.get('reason')}; pick one below")
                if best.get("needs_board_doc") and not self.auto_info[2]:
                    ctx["board_pick"] = {"kind": "board_doc", "label": "Use board document",
                                         "options": best["needs_board_doc"]}
                elif len(best.get("options") or []) > 1:
                    ctx["board_pick"] = {"kind": "ambiguous", "label": "Use this document",
                                         "options": best["options"]}
            elif self.auto_info is not None:
                ctx["board_warning"] = f"no Electronics board open in {program}"
            else:
                ctx["board_warning"] = "choosing…"
        elif mode == "auto" and self.auto_info and self.auto_info[0] \
                and self.auto_info[0].get("key") == tgt.get("key"):
            ctx["board_note"] = f"covers {self.auto_info[1]:.0%} of BOM placements"
        ok = bool(ctx.get("board_name")) and not ctx.get("fusion_error") and not ctx.get("board_warning")
        reason = ctx.get("fusion_error") or ctx.get("board_warning") or ""
        return ctx | {"_ok": ok, "_reason": reason, "_pick": ctx.get("board_pick")}

    def update_board_gate(self) -> None:
        c = self.board_ctx()
        self.ctl.board_ok, self.ctl.board_block_reason = c["_ok"], c["_reason"]
        self.board_pick = c["_pick"]

    # ------------------------------------------------------------ scan events (ui.on_live / on_event)
    def _on_live(self, d: dict) -> None:
        with self._lock:
            self.live = d
            st = d.get("status")
            if st in (Status.MATCH, Status.MATCH_NO_PLACEMENT, Status.NOT_IN_BOM):
                self._label_seen_t = 0.0
            elif len(d.get("codes", [])) >= 2:
                self._label_seen_t = time.monotonic()
        self._changed()

    def _on_stalled(self, info: dict) -> None:
        with self._lock:
            self.stall_info = info | {"t": time.monotonic()}
        self._changed()

    def _on_event(self, ev) -> None:
        """A fired scan (the debouncer decided). Highlight in Fusion with the PoC method, then tell Polybench."""
        with self._lock:
            if self._is_idle():
                return
            self._blocked = None
            if ev.action == "blocked":
                self._blocked = self.ctl.board_block_reason or f"no usable {self.program} board"
            elif ev.action == "highlight":
                self._submit_highlight(ev.fired, force=False)
        if self.on_fire:
            try:
                self.on_fire(ev, "camera")      # the server journals the scan, then calls mark_fired()
            except Exception:
                pass
        else:
            self.mark_fired("camera", ev.fired.mouser_pn or "", None, ev.fired.status == Status.MATCH)
        self._changed()

    ECAD_OFF_MSG = "ECAD disabled in step 1"

    def set_ecad_off(self, reason: str | None) -> None:
        """Step 1 chose 'Continue without ECAD': from now on nothing is sent to Fusion (no polls, picks, selections, clears)."""
        reason = reason or None
        with self._lock:
            if reason == self.ecad_off:
                return
            self.ecad_off = reason
            self._blocked = self.ECAD_OFF_MSG if reason else None
        self._changed()

    def clear_selection(self) -> dict:
        """Waiting for the next scan: drop the result card and clear the selection in Fusion with the PoC client's clear()."""
        with self._lock:
            self.ctl.last_fired, self.ctl.last_highlight = None, None
            self._pending_hl = None
            self._blocked = self.ECAD_OFF_MSG if self.ecad_off else None
            sent = not self.ecad_off and self.fusion is not None
            if sent:
                client = self.fusion
                self.fworker.submit(lambda: client.clear(), lambda r, e: self._changed(), force=True)
        self._changed()
        return {"ok": True, "sent": sent}

    def _submit_highlight(self, res: Resolution, force: bool) -> None:
        if self.ecad_off:
            self._blocked = self.ECAD_OFF_MSG
            return
        self._pending_hl = res
        ctl = self.ctl
        self.fworker.submit(lambda: ctl.do_highlight(res), lambda hr, e: self._on_highlight(res, hr, e), force=force)

    def _on_highlight(self, res, hr, err) -> None:
        if self._pending_hl is res:
            self._pending_hl = None
        if self._fusion_idle() and err == self.fworker.paused_error:
            return
        if err:  # do_highlight never raises; belt and braces
            hr = HighlightResult(ok=False, error=err)
            if res is self.ctl.last_fired:
                self.ctl.last_highlight = hr
        if hr and hr.error and "BOARD_NOT_OPEN" in hr.error:
            if self.fusion.mode == "auto":
                self.rescan_boards()
            else:
                self.poll_fusion()

    def select_targets(self, parts, pins=(), nets=(), timeout: float = 8.0) -> dict:
        """A test came into focus: select its parts, pins and nets in Fusion with the PoC client (FusionClient.highlight_targets), and
        wait for the answer. An explicit action, so it is sent even while the camera is paused. Returns the result card data."""
        if self.ecad_off:
            return {"ok": False, "disabled": True, "error": self.ECAD_OFF_MSG}
        if not parts and not pins and not nets:
            r = self.clear_selection()
            return {"ok": True, "highlighted": [], "cleared": True, "sent": r.get("sent")}
        self.ensure_board()
        end = time.monotonic() + 6.0          # the first call after a start can arrive while the board is still being chosen
        while True:
            with self._lock:
                ok, why = self.ctl.board_ok, self.ctl.board_block_reason
                client = self.fusion
            if ok or why not in self.pending_reasons or time.monotonic() > end:
                break
            self.update_board_gate()
            time.sleep(0.1)
        if not ok or client is None:
            with self._lock:
                pick = self.board_pick
            return {"ok": False, "error": f"not selected in {self.program}: " + (why or f"no usable {self.program} board"),
                    **({"pick": pick} if pick else {})}
        done, box = threading.Event(), {}

        def cb(r, e):
            box["r"], box["e"] = r, e
            done.set()
        self.fworker.submit(lambda: client.highlight_targets(parts, pins, nets), cb, force=True)
        if not done.wait(timeout):
            return {"ok": False, "error": f"{self.program} did not answer in time; the app keeps working"}
        r, e = box.get("r"), box.get("e")
        if e or r is None:
            return {"ok": False, "error": e or f"no answer from {self.program}"}
        if not r.ok:
            return {"ok": False, "error": r.error}
        self._changed()
        return {"ok": True, "highlighted": list(r.highlighted), "missing": list(r.not_on_board), "pins": list(r.pins),
                "pins_missing": list(r.pins_missing), "nets": list(r.nets), "nets_missing": list(r.nets_missing),
                "net_mode": r.net_mode, "note": r.note}

    def select_line(self, line: int, source: str = "typed", fmt_codes=None) -> dict:
        """The operator picked a BOM line (typed scan, wedge, or the to-do list): show the PoC result for it and select its
        designators in Fusion with the same call a camera scan uses. Returns immediately; the outcome is in state()."""
        with self._lock:
            lines = self.line_map.get(int(line), ())
            if not lines:
                return {"ok": False, "error": "line has no Mouser P/N in the scanner BOM"}
            fitted = [l for l in lines if l.fitted]
            seen: set = set()
            desigs = tuple(d for l in fitted for d in l.designators if not (d in seen or seen.add(d)))
            status = Status.MATCH if desigs else Status.MATCH_NO_PLACEMENT
            res = Resolution(status, lines[0].mouser_pn, tuple(lines), desigs, tuple(fmt_codes or ()))
            self.ctl.last_fired, self.ctl.last_highlight = res, None
            self._blocked = None
            if status == Status.MATCH:
                # ensure_board() returns as soon as board_ctx() sees the auto-picked target, but ctl.board_ok is only refreshed by the
                # worker's _on_auto/_on_poll callback, which can still be queued: recompute the gate here, from the same state.
                self.update_board_gate()
                if self.ecad_off:
                    self._blocked = self.ECAD_OFF_MSG
                elif not self.ctl.board_ok or self.fusion is None:
                    self._blocked = self.ctl.board_block_reason or f"no usable {self.program} board"
                else:
                    self._submit_highlight(res, force=True)
        self._changed()
        return {"ok": True, "status": status.value}

    # ------------------------------------------------------------ state for the page (ui.hud_ctx / refresh_hud)
    def hud_ctx(self) -> dict:
        ctx = self.board_ctx()
        if self.bom is not None:
            ctx["bom_name"] = self.bom_name
            ctx["bom_summary"] = bom_summary(self.bom)
        if self.bom_error:
            ctx["bom_error"] = self.bom_error
        cam = self.camera
        if cam is not None and cam.state == "error":
            ctx["camera_ok"], ctx["camera_error"] = False, cam.error
        else:
            ctx["camera_ok"] = True
        si = self.stall_info
        if si is not None and time.monotonic() - si["t"] < 10:
            ctx["scanner_error"] = (f"STALLED: a decode hung > {si['ms'] / 1000:.1f} s; "
                                    f"restarted the scanner (stall #{si['count']})")
        if si is not None and si["count"] >= 3:
            ctx["scanner_error"] = (f"STALLED {si['count']} times: hung decodes keep a CPU core "
                                    "busy. Stop and Start the scanner, or restart the server.")
        if cam is not None and cam.state == "running" and "t" in self.live and not self._is_idle() \
                and time.monotonic() - self.live["t"] > 3 * ScanWorker.STALL_S:
            ctx.setdefault("scanner_error", "no decode result for "
                           f"{time.monotonic() - self.live['t']:.0f} s while frames arrive")
        if self.bom is not None and time.monotonic() - self._label_seen_t < 1.2:
            ctx["label_seen"] = True
        res = self.ctl.last_fired
        if res is not None and res.status == Status.MATCH:
            if self._blocked and self.ctl.last_highlight is None and self._pending_hl is None:
                ctx["blocked_reason"] = self._blocked
            ctx["pending"] = self._pending_hl is res
        return ctx

    def state(self) -> dict:
        with self._lock:
            ctx = self.hud_ctx()
            top, bottom = hud_sections(self.ctl.last_fired, self.ctl.last_highlight, ctx)
            cam = self.camera
            bits = []
            if cam is not None:
                if cam.state == "running" and cam.size:
                    bits.append(f"CAM {cam.index} {cam.size[0]}x{cam.size[1]} {cam.fps_measured:.0f}fps")
                else:
                    bits.append(f"CAM {cam.index}: {cam.state}")
            if self._background:
                bits.append("Paused (page in background)")
            if self.ecad_off:
                bits.append(self.ECAD_OFF_MSG)
            age = time.monotonic() - self.live["t"] if "t" in self.live else None
            if self.live.get("error"):
                bits.append(self.live["error"])
            elif "ms" in self.live and age is not None and age <= ScanWorker.STALL_S:
                bits.append(f"decode {self.live['ms']:.0f} ms · {len(self.live.get('codes', []))} codes "
                            f"in view · {age:.1f} s ago")
            elif age is not None and cam is not None and cam.state == "running" and not self._is_idle():
                bits.append(f"no decode result for {age:.0f} s")
            if self.fusion_status.get("connected"):
                lat = getattr(self.fusion, "last_latency_ms", None)
                bits.append(f"{self.program} connected" + (f" ({lat:.0f} ms)" if lat else ""))
            if self.settings_err:
                bits.append(self.settings_err)
            roi = None
            if cam is not None and cam.size:
                x, y, w, h = scanner.roi_rect(*cam.size)
                roi = [x / cam.size[0], y / cam.size[1], w / cam.size[0], h / cam.size[1]]
            res = self.ctl.last_fired
            tgt = self.fusion.target or {}
            return {
                "enabled": self.enabled, "paused": self._manual_paused, "idle": self._idle,
                "background": self._background, "ecad_off": self.ecad_off, "program": self.program,
                "mirror": bool(self.worker.flip_camera),
                "camera": {"state": cam.state if cam else "none", "error": cam.error if cam else None,
                           "index": int(self.settings.camera_index), "size": list(cam.size) if cam and cam.size else None,
                           "fps": round(cam.fps_measured, 1) if cam else 0, "roi": roi, "options": self.cameras()},
                "board": {"mode": self.fusion.mode, "name": tgt.get("name"), "key": tgt.get("key"),
                          "options": self.boards, "ok": ctx["_ok"], "reason": ctx["_reason"],
                          "connected": bool(self.fusion_status.get("connected")), "pinned": self._pinned_key,
                          "pick": self.board_pick},
                "bom": {"name": self.bom_name, "summary": bom_summary(self.bom) if self.bom else "", "error": self.bom_error},
                "hud": {"top": [list(i) for i in top], "bottom": [list(i) for i in bottom], "status": " · ".join(bits)},
                "result": None if res is None else {
                    "status": res.status.value, "mouser_pn": res.mouser_pn,
                    "mpn": list(dict.fromkeys(l.mpn for l in res.lines if l.mpn)),
                    "description": list(dict.fromkeys(l.description for l in res.lines if l.description)),
                    "designators": list(res.designators),
                    "highlighted": list(self.ctl.last_highlight.highlighted) if self.ctl.last_highlight else None,
                    "not_on_board": list(self.ctl.last_highlight.not_on_board) if self.ctl.last_highlight else None,
                    "fusion_error": self.ctl.last_highlight.error if self.ctl.last_highlight else None,
                    "pending": self._pending_hl is res, "blocked": self._blocked,
                    "board": tgt.get("name")},
                "fired": self.fired_info,
            }

    def mark_fired(self, source: str, code: str, line, ok: bool) -> dict:
        """Called by the server after it journalled a scan: the page reacts to a new seq by moving to the line."""
        with self._lock:
            self.fire_seq += 1
            self.fired_info = {"seq": self.fire_seq, "source": source, "code": code, "line": line, "ok": ok}
            return self.fired_info
