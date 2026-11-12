"""Startup resilience: a master test plan that no longer loads (for example after a plan edit dropped tests that an action
`proposes`) must not brick the app. Start with the last good plan from `<batch>/ai/plan_history/`, keep the master file
untouched, and say so in a red banner and on the console with the exact command that restores it."""
from __future__ import annotations

from .. import platform as _platform
import time
from pathlib import Path

from ..protocol import load


def history_dir(data_dir: Path, proto, batch: str | None) -> Path:
    name = batch or f"{proto.data['design']['name']}_{time.strftime('%Y-%m-%d')}"
    return Path(data_dir) / name / "ai" / "plan_history"


def master_of(proto) -> Path | None:
    files = (proto.data.get("test_plan") or {}).get("files") or []
    return Path(files[0]["path"]) if files else None


def try_recover(proto, protocol_path, data_dir, batch: str | None):
    """(protocol, info) when the last good plan in the history loads cleanly, else (None, info) with the commands to fix it by hand."""
    master = master_of(proto)
    errors = [str(i) for i in proto.errors]
    info = {"error": errors[0] if errors else "", "errors": errors[:5], "master": str(master) if master else None,
            "lint_command": f"polybench plan lint {_platform.shell_quote(str(master))} --protocol {_platform.shell_quote(str(protocol_path))}" if master else ""}
    if not master or not (proto.data.get("test_plan") or {}).get("generate"):
        return None, info
    hist = history_dir(data_dir, proto, batch)
    cands = sorted(hist.glob("*.md"), key=lambda p: p.stat().st_mtime_ns, reverse=True) if hist.is_dir() else []
    info["history_dir"] = str(hist)
    for c in cands:
        p = load(protocol_path, plan_override=c)
        if p.errors:
            continue
        p.data["test_plan"]["files"][0].update(path=str(master), name=master.name, exists=master.is_file())   # drafts and approvals still act on the master
        info.update(restored_from=str(c), command=_platform.restore_file_command(str(master), str(c)),
                    command_note=f"the path contains a % sign, which cmd.exe cannot take safely in a command: copy {c} over {master} by hand (keep the broken one as {master}.broken first)")
        return p, info
    return None, info


def console_text(info: dict, recovered: bool) -> str:
    out = [f"The master test plan does not load: {info.get('error') or 'see the lint'}"]
    if recovered:
        out += [f"STARTED WITH THE LAST GOOD PLAN: {info['restored_from']}",
                f"The master file {info['master']} was NOT changed. To restore the last good plan as the master, run:",
                "  " + (info["command"] or info["command_note"]),
                "Or approve a corrected draft in design step 4."]
    else:
        out += [f"There is no earlier plan to fall back to ({info.get('history_dir') or 'no ai/plan_history for this batch'}).",
                "Find the problem with:", "  " + info.get("lint_command", "polybench lint <protocol>"),
                "then restore the plan from your editor's history or git."]
    return "\n".join(out)
