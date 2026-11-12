"""ECAD bridge `altium`: interface only (`kicad` is the real bridge in polybench.kicad). They report 'not installed' and never touch anything."""
from __future__ import annotations


class _NotInstalled:
    name = ""
    program = ""

    def status(self) -> dict:
        return {"connected": False, "program": self.program, "document": None, "editor": None,
                "detail": f"{self.program} bridge not installed (interface only)"}

    def highlight(self, designators: list[str]) -> dict:
        return {"ok": False, "highlighted": [], "missing": [], "message": f"{self.program} bridge not installed"}

    def clear(self) -> None:
        return None

    def designators(self) -> list[str] | None:
        return None


class AltiumBridge(_NotInstalled):
    name = "altium"
    program = "Altium Designer"
