"""AOI actions. Review assistance only: nothing here computes a verdict; operator decisions do."""
from __future__ import annotations

import json
from pathlib import Path


class _Aoi:
    capabilities: list[str] = []

    def safe_state(self):
        return "holds nothing (offline image analysis)"

    def selftest(self):
        from polybench import extras
        extras.need("aoi")
        import polybench.aoi.protocol_run  # noqa: F401   imports cv2 and numpy
        return True


class AoiRun(_Aoi):
    name = "aoi.run"

    def run(self, ctx):
        from polybench import extras
        from polybench.plugin_api import Cancelled
        extras.need("aoi")        # a bare install: "install polybench-pcb-validation[aoi] to use this", not a traceback
        from polybench.aoi import runner
        cfg = ctx.params.get("_aoi")
        if not cfg:
            raise RuntimeError("this protocol has no aoi: block")
        batch = ctx.workdir.parent.parent

        def log(text, level="info"):
            if isinstance(level, str) and level not in ("info", "warn", "error"):
                level = "info"
            ctx.log(str(text), level)
            if ctx.cancelled():
                raise Cancelled("AOI run cancelled")

        ctx.progress(0.02, "loading photos")
        out = runner.run(batch, ctx.workdir, cfg, log, simulate=ctx.simulate)
        ctx.progress(1.0, f"{len(out['items'])} candidates")
        ctx.artifact("aoi_run", {**out, "run": None})
        ctx.log(f"AOI done: {len(out['items'])} issue candidates to review; no board is certified good by AOI.")


class AoiLsExport(_Aoi):
    name = "aoi.ls_export"

    def run(self, ctx):
        from polybench.aoi import runner, ls_project
        raise RuntimeError("Label Studio export is wired in the ls_project module; see docs/AOI.md")
