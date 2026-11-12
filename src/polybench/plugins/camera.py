"""Action `camera.snapshot` (stretch): one JPEG from a UVC camera into the run folder, proposed as evidence.

params: index (0), width/height (optional), warmup_frames (5), map {evidence} -> test id (proposes the file name as text),
        quality (90). Needs OpenCV (`pip install opencv-python`); without it the action says so. Simulator: writes a small
        generated JPEG (Pillow if present, else a built-in 1x1 JPEG).
"""
from __future__ import annotations

import base64
from pathlib import Path

from ._util import ParamError, PluginError, safe_name, stamp, to_int

_TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////"
    "/8AACwgAAQABAREA/8QAFAABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAAPwBH/9k=")


class CameraSnapshot:
    capabilities = ["camera"]
    name = "camera.snapshot"

    def run(self, ctx) -> None:
        p = ctx.params
        idx = to_int(p.get("index", 0), "index")
        quality = to_int(p.get("quality", 90), "quality")
        out = Path(ctx.workdir) / f"{safe_name(ctx.board)}_photo_{stamp()}.jpg"
        out.parent.mkdir(parents=True, exist_ok=True)
        if ctx.simulate:
            try:
                from PIL import Image, ImageDraw
                im = Image.new("RGB", (640, 360), (40, 60, 80))
                ImageDraw.Draw(im).text((20, 20), f"simulated photo {ctx.board}", fill=(230, 230, 230))
                im.save(out, "JPEG", quality=quality)
            except ImportError:
                out.write_bytes(_TINY_JPEG)
            ctx.log(f"simulated photo {out}")
        else:
            try:
                import cv2
            except ImportError:
                raise PluginError("OpenCV is not installed: install `polybench-pcb-validation[camera]` to use this") from None
            cap = cv2.VideoCapture(idx)
            try:
                if not cap.isOpened():
                    raise PluginError(f"camera index {idx} cannot be opened. On macOS allow camera access for the "
                                      "terminal / Python in System Settings > Privacy & Security > Camera.")
                if p.get("width"):
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, to_int(p["width"]))
                if p.get("height"):
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, to_int(p["height"]))
                frame = None
                for _ in range(max(1, to_int(p.get("warmup_frames", 5)))):
                    ok, frame = cap.read()
                    if ctx.cancelled():
                        raise PluginError("cancelled")
                if not ok or frame is None:
                    raise PluginError(f"camera {idx} opened but gave no frame")
                if not cv2.imwrite(str(out), frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality]):
                    raise PluginError(f"could not write {out}")
            finally:
                cap.release()
            ctx.log(f"photo saved to {out}")
        mp = p.get("map") or {}
        if mp.get("evidence"):
            ctx.propose(mp["evidence"], out.name, None, str(out), str(out))
        ctx.progress(1.0, "done")

    def safe_state(self) -> str:
        return "no camera handle is held by this process (released when the action ends)"

    def selftest(self) -> bool:
        return True
