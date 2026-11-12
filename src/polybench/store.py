"""Append-only, fsynced, hash-chained JSON-lines journal."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path


def default_data_dir() -> Path:
    return Path(os.environ.get("POLYBENCH_DATA", "~/polybench-data")).expanduser()


class Journal:
    """One journal.jsonl per batch. append() returns only after write + flush + fsync."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.events: list[dict] = []
        self.problems: list[str] = []
        self._last_hash = "0" * 64
        self._seq = 0
        self._load()
        self._fh = open(self.path, "ab", buffering=0)

    @staticmethod
    def _hash(prev: str, ev: dict) -> str:
        body = {k: v for k, v in ev.items() if k != "hash"}
        return hashlib.sha256((prev + json.dumps(body, sort_keys=True, separators=(",", ":"))).encode()).hexdigest()

    def _load(self):
        if not self.path.exists():
            return
        raw = self.path.read_bytes()
        good_end = 0
        pos = 0
        for line in raw.splitlines(keepends=True):
            pos += len(line)
            text = line.strip()
            if not text:
                good_end = pos
                continue
            try:
                ev = json.loads(text)
            except ValueError:
                # torn last line from a kill mid-write: drop it from memory, keep the file bytes intact
                self.problems.append(f"line {len(self.events) + 1}: unreadable (torn write?), ignored")
                break
            if ev.get("prev_hash") != self._last_hash:
                self.problems.append(f"event {ev.get('id')}: hash chain broken (file edited?)")
            elif self._hash(self._last_hash, ev) != ev.get("hash"):
                self.problems.append(f"event {ev.get('id')}: hash mismatch (file edited?)")
            self._last_hash = ev.get("hash", self._last_hash)
            self._seq = max(self._seq, int(ev.get("id", 0)))
            self.events.append(ev)
            good_end = pos
        if good_end < len(raw) and self.problems and "torn" in self.problems[-1]:
            # truncate the torn tail so the next append starts on a clean line
            with open(self.path, "r+b") as f:
                f.truncate(good_end)

    def append(self, type_: str, payload: dict, operator: str = "") -> dict:
        with self.lock:
            self._seq += 1
            ev = {"id": self._seq, "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + f".{int(time.time() * 1000) % 1000:03d}",
                  "type": type_, "operator": operator, "payload": payload, "prev_hash": self._last_hash}
            ev["hash"] = self._hash(self._last_hash, ev)
            data = (json.dumps(ev, separators=(",", ":")) + "\n").encode()
            self._fh.write(data)
            os.fsync(self._fh.fileno())
            self._last_hash = ev["hash"]
            self.events.append(ev)
            return ev

    @property
    def head_hash(self) -> str:
        return self._last_hash

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass
