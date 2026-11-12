"""Photos named by the protocol: `aoi: {photos: {D01: aoi/D01_top.jpg, D02: {top: ..., bottom: ...}}}`.

When a batch has never had an AOI photo (no `aoi_photo`, `aoi_photo_move` or `aoi_photo_removed` event in its journal) and a named board
exists in it, each file is copied into `<batch>/aoi/photos/` (metadata removed, as for an upload) and journalled as `aoi_photo` with
`assigned_by: protocol`. Paths are relative to the file that holds the `aoi:` block. A missing file, an unknown board or a file that is not
a photo is skipped and reported; the operator's tiles, drops and removals always win, and a batch that had photos is never seeded again.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import link, privacy


def _wanted(cfg: dict):
    for board, spec in (cfg.get("photos") or {}).items():
        spec = {"top": spec} if isinstance(spec, str) else dict(spec or {})
        for side, rel in spec.items():
            if side in ("top", "bottom") and isinstance(rel, str):
                yield str(board), side, rel


def seed_photos(eng, batch_dir) -> list[dict]:
    """Returns one dict per photo considered: {board, side, file, done: bool, why}. Idempotent."""
    cfg = eng.proto.data.get("aoi") or {}
    if not cfg.get("photos") or cfg.get("_seeded_checked"):
        return []
    if any(ev.get("type") in ("aoi_photo", "aoi_photo_move", "aoi_photo_removed") for ev in eng.journal.events):
        return []
    labels = {b["label"] for b in eng.state.boards}
    out = []
    base = Path(cfg.get("_dir") or ".")
    for board, side, rel in _wanted(cfg):
        rec = {"board": board, "side": side, "file": rel, "done": False, "why": ""}
        out.append(rec)
        src = (base / rel).resolve()
        if board not in labels:
            rec["why"] = "no such board in this batch"
        elif src.suffix.lower() not in link.PHOTO_SUFFIXES or not src.is_file():
            rec["why"] = "not a photo file"
        else:
            try:
                data, name, _ = privacy.strip_metadata(src.read_bytes(), src.name)
            except Exception as e:  # noqa: BLE001
                rec["why"] = f"cannot read the photo ({type(e).__name__})"
                continue
            sha = hashlib.sha256(data).hexdigest()
            dest = f"aoi/photos/{sha[:10]}_{name}"
            (Path(batch_dir) / dest).parent.mkdir(parents=True, exist_ok=True)
            (Path(batch_dir) / dest).write_bytes(data)
            eng.aoi_add_photo(board, side, name, dest, sha, len(data), "protocol", "protocol")
            rec["done"] = True
    return out
