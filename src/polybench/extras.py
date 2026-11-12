"""Optional extras: what a bare `pip install polybench-pcb-validation` does not bring, and the one message for it.

A feature that needs an extra calls `need("aoi")` (or `check("aoi")`) before it imports cv2, PIL, zxingcpp or openpyxl.
The server turns MissingExtra into a JSON answer with status 424 and `missing_extra`; the page shows the text, never a
traceback. Importing this module imports nothing optional."""
from __future__ import annotations

import importlib
import importlib.util

PACKAGE = "polybench-pcb-validation"

# extra -> the import names that must be present. Mirrors [project.optional-dependencies] in pyproject.toml.
EXTRAS: dict[str, tuple[str, ...]] = {
    "aoi": ("cv2", "PIL", "numpy"),
    "camera": ("cv2",),
    "scanner": ("cv2", "zxingcpp", "openpyxl", "PIL", "numpy"),
    "voice": ("numpy", "sounddevice"),
    "esp32": ("esptool",),
    "dmm": ("hid",),
}
WHAT = {"aoi": "the Visual step AOI and photo import", "camera": "camera.snapshot", "scanner": "the Assembly Scanner",
        "voice": "hands-free voice", "esp32": "the ESP32 flash action", "dmm": "the live multimeter and dmm.read"}


class MissingExtra(RuntimeError):
    def __init__(self, extra: str, missing: tuple[str, ...] = ()):
        self.extra = extra
        self.missing = tuple(missing)
        super().__init__(message(extra))


def message(extra: str) -> str:
    return f"install `{PACKAGE}[{extra}]` to use this ({WHAT.get(extra, extra)})"


def missing(extra: str) -> list[str]:
    out = []
    for m in EXTRAS[extra]:
        try:
            if importlib.util.find_spec(m) is None:
                out.append(m)
        except (ImportError, ValueError):
            out.append(m)
    return out


def check(extra: str) -> bool:
    return not missing(extra)


def need(extra: str) -> None:
    """Raise MissingExtra unless every module of the extra can be imported. Cheap: find_spec imports nothing."""
    m = missing(extra)
    if m:
        raise MissingExtra(extra, tuple(m))


class _Lazy:
    """Stands in for an optional module until the first attribute access, which imports it (or raises MissingExtra)."""

    def __init__(self, name: str, extra: str):
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_extra", extra)
        object.__setattr__(self, "_mod", None)

    def _load(self):
        mod = object.__getattribute__(self, "_mod")
        if mod is None:
            name, extra = object.__getattribute__(self, "_name"), object.__getattribute__(self, "_extra")
            try:
                mod = importlib.import_module(name)
            except ImportError as e:
                raise MissingExtra(extra, (name,)) from e
            object.__setattr__(self, "_mod", mod)
        return mod

    def __getattr__(self, attr):
        return getattr(self._load(), attr)


def lazy(name: str, extra: str) -> _Lazy:
    """`cv2 = lazy("cv2", "scanner")`: the module imports on first use, and a missing one names the extra to install."""
    return _Lazy(name, extra)


def install_command(extra: str) -> str:
    """The pip command that brings an extra (also used by the Plugins panel). Shown to copy, never run by the app."""
    return f'pip install "{PACKAGE}[{extra}]"'


def pipx_command(extra: str) -> str:
    """The same for an install made with pipx (the extra is added to the existing pipx environment)."""
    return f'pipx install --force "{PACKAGE}[{extra}]"'
