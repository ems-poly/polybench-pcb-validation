"""Stand-ins for the two pieces of real hardware, used by tests, demos and --simulate: a camera that serves recorded images
and a Fusion client that holds a board in memory. Both keep the real classes' interfaces, so the service runs unchanged.
Neither is ever picked for a real batch by itself; see build_service() in polybench_link.py.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from polybench.extras import lazy as _lazy
cv2 = _lazy("cv2", "scanner")
np = _lazy("numpy", "scanner")

from polybench.assembly_scanner.models import HighlightResult


_os_environ_get = os.environ.get
RECORDING = Path(__file__).with_name("sim_demo_reads.json")


class ImageCamera:
    """Camera interface (start/stop/latest/state/error/size/fps_measured/index) over recorded frames.

    `source` is a folder. The file named in <folder>/CURRENT is the frame shown (empty or missing: a grey frame with no
    code), so a test or a demo switches the 'camera view' by writing that file. Photos are scaled to 1920 px wide.
    """

    def __init__(self, index: int, source: str | Path):
        self.index = index
        self.source = Path(source)
        self.size = None
        self.fps_measured = 15.0
        self.state = "stopped"
        self.error = None
        self._seq = 0
        self._cache: dict[str, np.ndarray] = {}
        self._lock = threading.Lock()
        self.starts = 0
        self.stops = 0

    def start(self) -> None:
        self.state, self.error = "running", None
        self.starts += 1

    def stop(self, timeout: float = 2.0) -> None:
        self.state = "stopped"
        self.stops += 1

    def _load(self, name: str) -> np.ndarray:
        if name not in self._cache:
            if not name:
                img = np.full((720, 1280, 3), 90, np.uint8)
            else:
                p = self.source / name
                if p.suffix.lower() in (".heic", ".heif"):
                    from PIL import Image, ImageOps
                    import pillow_heif
                    pillow_heif.register_heif_opener()
                    im = ImageOps.exif_transpose(Image.open(p)).convert("RGB")
                    img = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
                else:
                    img = cv2.imread(str(p))
                if img is None:
                    raise FileNotFoundError(p)
                if img.shape[1] > 1920:
                    s = 1920 / img.shape[1]
                    img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
            self._cache[name] = img
        return self._cache[name]

    def latest(self):
        if self.state != "running":
            return self._seq, None
        try:
            name = (self.source / "CURRENT").read_text(encoding="utf-8").strip()
        except OSError:
            name = ""
        time.sleep(0.03)
        with self._lock:
            self._seq += 1
            img = self._load(name)
            self.size = (img.shape[1], img.shape[0])
            return self._seq, img.copy()


class SimulatedFusionClient:
    """FusionClient interface over an in-memory board. Auto board choice by placement coverage, selection kept in `selected`."""

    def __init__(self, designators, name="DEMO-1 (simulated)", netlist=None, net_support=True, pinref_support=True):
        import json as _json
        import os as _os
        self.name = name
        if netlist is None and _os.environ.get("POLYBENCH_SIM_NETLIST"):
            netlist = _json.loads(Path(_os.environ["POLYBENCH_SIM_NETLIST"]).read_text(encoding="utf-8"))
        self.netlist = netlist
        self.net_support = net_support and _os.environ.get("POLYBENCH_SIM_NET_SUPPORT", "1") != "0"
        self.pinref_support = pinref_support and _os.environ.get("POLYBENCH_SIM_PINREF_SUPPORT", "1") != "0"
        self.known = {str(d).upper(): str(d) for d in designators}
        self.selected: tuple[str, ...] = ()
        self.history: list[tuple[str, tuple[str, ...]]] = []
        self.last_latency_ms = 1.0
        self.last_info: dict = {}
        self._pinned = None
        self._auto = None
        self._board = {"key": "sim:board", "name": name, "ui_name": name, "has_board": True, "is_active": True,
                       "designator_count": len(self.known)}

    @property
    def mode(self):
        return "manual" if self._pinned else "auto"

    @property
    def target(self):
        return self._pinned or self._auto

    def status(self):
        return {"connected": True, "doc_name": self.name, "is_electronics": True, "mode": self.mode, "board": self._board,
                "boards": 1, "open_boards": [dict(self._board)], "active_doc": self.name, "error": None}

    def list_boards(self):
        return [dict(self._board)]

    def select_board(self, doc):
        self._pinned = None if doc is None else dict(self._board)
        return self._pinned

    def auto_select_for(self, designators, pin=True, boards=None):
        want = {str(d).upper() for d in designators}
        m = want & set(self.known)
        cov = len(m) / len(want) if want else 0.0
        best = dict(self._board, coverage=cov, precision=1.0, matched=len(m), missing=sorted(want - set(self.known)),
                    confident=cov >= 0.95, reason="ok" if cov >= 0.95 else f"best coverage {cov:.0%} < 95%")
        if pin:
            self._auto = best if best["confident"] else None
        return best, cov, None

    def highlight(self, designators):
        want = [str(d).strip() for d in designators if str(d).strip()]
        have = tuple(self.known[d.upper()] for d in want if d.upper() in self.known)
        self.selected = have
        self.history.append(("highlight", tuple(want)))
        return HighlightResult(ok=True, highlighted=have, not_on_board=tuple(d for d in want if d.upper() not in self.known))

    def highlight_targets(self, parts, pins=(), nets=()):
        """Stand-in for FusionClient.highlight_targets. With a netlist ({net: ["IC1.12", ...]}, from POLYBENCH_SIM_NETLIST or the
        constructor) pins and nets are checked against it; without one every net and every pin of a known part counts as found.
        `net_support` False simulates a Fusion that rejects net objects (the pin-reference fallback is used)."""
        parts = [str(d).strip() for d in parts if str(d).strip()]
        pins = [str(d).strip() for d in pins if str(d).strip()]
        nets = [str(d).strip() for d in nets if str(d).strip()]
        nl = self.netlist
        pin_net = {}
        for pn in pins:
            des = pn.split(".")[0]
            net = None
            if nl is not None:
                net = next((n for n, ps in nl.items() if pn in ps), None)
            elif des.upper() in self.known:
                net = "(unchecked)"
            pin_net[pn] = net
        pins_ok = tuple(p for p, n in pin_net.items() if n)
        pins_missing = tuple(p for p, n in pin_net.items() if not n)
        nets_ok = tuple(n for n in nets if nl is None or n in nl)
        nets_missing = tuple(n for n in nets if nl is not None and n not in nl)
        want = list(dict.fromkeys(parts + [p.split(".")[0] for p in pins]))
        mode = ""
        extra = []
        if nets_ok:
            mode = "nets" if self.net_support else "pinrefs" if self.pinref_support else "parts"
            if mode == "parts" and nl is not None:
                extra = [p.split(".")[0] for n in nets_ok for p in nl.get(n, [])]
        elif pins_ok:
            mode = "pinrefs"
        have = tuple(self.known[d.upper()] for d in dict.fromkeys(want + extra) if d.upper() in self.known)
        self.selected = have
        self.history.append(("targets", {"parts": tuple(parts), "pins": tuple(pins), "nets": tuple(nets), "mode": mode}))
        log = _os_environ_get("POLYBENCH_SIM_HIGHLIGHT_LOG")          # test aid: one JSON line per selection that reaches the (simulated) Fusion
        if log:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"parts": parts, "pins": pins, "nets": nets, "mode": mode, "selected": list(have)}) + "\n")
        note = "simulated: nothing is drawn" + ("" if nl is not None else "; no netlist, nets and pins are not checked")
        if nets_ok and mode == "parts":
            note += "; Fusion cannot select a net here: the parts with a pin on the net are selected instead"
        return HighlightResult(ok=True, highlighted=have, not_on_board=tuple(d for d in want if d.upper() not in self.known),
                               pins=pins_ok, pins_missing=pins_missing, nets=nets_ok, nets_missing=nets_missing, net_mode=mode, note=note)

    def clear(self):
        self.selected = ()
        self.history.append(("clear", ()))
        return HighlightResult(ok=True)

    def footprints(self, designators):
        """Placed footprints from a recorded Fusion read (sanitised: demo values and net names), replayed through the same
        reader the real client uses. Designators the recording does not hold come back as errors, so their card shows amber."""
        import json
        from polybench import footprints as fpm
        rec = json.loads(RECORDING.read_text(encoding="utf-8"))

        def read(entity, obj=None):
            args = {"entity_type": entity}
            if obj:
                args["object"] = obj
            got = rec.get(json.dumps(args, sort_keys=True))
            if got is None:
                if entity in ("electronics.Element", "electronics.Wire", "electronics.Circle", "electronics.Contact", "electronics.Package"):
                    return {"items": [], "pagination": {"hasMore": False}}
                raise fpm.FootprintError("simulated ECAD has no data for this part")
            return got
        return {"ok": True, "document": self.name, "key": self._board["key"], "parts": fpm.read_footprints(read, list(designators)), "error": None}
