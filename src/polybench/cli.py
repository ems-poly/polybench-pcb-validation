"""polybench CLI: serve (default), lint, plan, selftest, doctor, example."""
from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from pathlib import Path


def cmd_lint(path) -> int:
    from .protocol import load
    p = load(path)
    for i in p.issues:
        print(i)
    n_t = sum(1 for _ in p.tests())
    print(f"{p.path.name}: {len(p.data['steps'])} steps, {n_t} tests, {len(p.data['bom'])} BOM lines, "
          f"{len(p.errors)} errors, {len(p.warnings)} warnings, hash {p.hash}")
    return 2 if p.errors else 0


def _find_protocol(plan: Path):
    """The top protocol file that points at this plan (looked for next to the plan and in its polybench/ folder)."""
    import yaml
    for d in (plan.parent, plan.parent / "polybench"):
        for f in sorted(d.glob("*.pcbv.yaml")):
            try:
                top = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            tp = top.get("test_plan")
            name = tp.get("file") if isinstance(tp, dict) else tp
            if name and (f.parent / name).resolve() == plan.resolve():
                return f
    return None


def cmd_plan(argv) -> int:
    """polybench plan import|lint <plan.md> [--protocol P] [--out FRAGMENT]"""
    from . import planfmt
    from .protocol import load
    ap = argparse.ArgumentParser(prog="polybench plan", description="test plan as master: lint the plan and generate protocol steps from it")
    ap.add_argument("action", choices=["import", "lint"])
    ap.add_argument("plan", help="the test plan markdown (plan_format: 1, docs/TEST_PLAN_FORMAT.md)")
    ap.add_argument("--protocol", help="top protocol file: gives the BOM (designator checks) and the action library; found automatically "
                                       "when a *.pcbv.yaml next to the plan points at it")
    ap.add_argument("--out", help="write the generated fragment here (default: standard output; nothing is written when the plan has errors)")
    a = ap.parse_args(argv)
    plan_path = Path(a.plan).expanduser().resolve()
    pf = Path(a.protocol).expanduser() if a.protocol else _find_protocol(plan_path)
    bom = lib = None
    if pf:
        proto = load(pf, check_plugins=False, plan_generate=False)
        bom = proto.data["bom"]
        lib = {x["id"]: x for s in proto.data["steps"] for sec in s["sections"] for x in sec.get("actions", [])}
        print(f"# protocol: {pf}", file=sys.stderr)
    else:
        print("# no protocol found: designators and actions are not checked (use --protocol)", file=sys.stderr)
    plan = planfmt.parse(plan_path, bom, set(lib) if lib is not None else None)
    for i in plan.issues:
        print(i, file=sys.stderr)
    full_errors = 0
    if pf and a.action == "lint":         # the full check startup runs: this plan loaded as the master plan of the protocol
        seen = {str(i) for i in plan.issues}
        full = load(pf, plan_override=plan_path)
        for i in full.issues:
            if str(i) not in seen:
                print(i, file=sys.stderr)
        full_errors = len(full.errors)
        print(f"# protocol load with this plan: {full_errors} errors, {len(full.warnings)} warnings", file=sys.stderr)
    n = sum(1 for _ in plan.tests())
    print(f"# {plan_path.name}: {n} tests in {sum(len(s.groups) for s in plan.sections if s.kind == 'tier')} groups, "
          f"{len(plan.errors)} errors, {len(plan.warnings)} warnings, hash {plan.hash}", file=sys.stderr)
    if plan.errors or full_errors:
        return 2
    if a.action == "lint":
        return 0
    text = planfmt.to_yaml(plan, lib)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"# wrote {a.out}", file=sys.stderr)
    else:
        print(text)
    return 0


def _check_adoption(info: dict, batch: str | None, proto_path: str) -> None:
    """Adopting a folder of batches under this protocol: every batch in the folder must have been recorded for the same design
    (the same check as POST /api/project/open and /adopt); `batch` only names which one starts."""
    from .protocol import load
    from .server import adoption_mismatch
    if not info.get("batches"):
        return
    pick = info["batches"]
    pr = load(proto_path)
    if pr.errors:
        return                                  # reported by the normal start
    msg = adoption_mismatch(pick, pr.data["design"]["name"])
    if msg:
        raise SystemExit(msg)


def resolve_start(a):
    """Which project, batch, protocol and ECAD platform this run opens (Project page model, PP-05).

    --protocol given (the command line used so far): the project folder is --data-dir (default ~/polybench-data). Without a
        settings file there it is adopted in place: polybench-project.json is written next to the batch folders, nothing moves.
        With one, --protocol / --ecad / --batch override it for this run only.
    no --protocol: --data-dir if given, else the last project in the recent list (~/.config/polybench/recent.json), with the
        batch it was last opened on; the settings file gives the protocol and the ECAD platform. With neither (first run, or
        a recent list with no usable project) there is no project: the server starts on the welcome page.
    Returns (workspace, batch, ecad_override, protocol_override, adopted); workspace is None for the welcome page.
    """
    from . import workspace as wslib
    from .store import default_data_dir
    if a.protocol:
        d = Path(a.data_dir).expanduser() if a.data_dir else default_data_dir()
        info = wslib.inspect(d)
        if info["kind"] == "batch":
            raise SystemExit(f"{d} is a batch folder; --data-dir is the project folder that holds the batch folders")
        try:
            ws = wslib.Workspace(d)
        except wslib.SettingsUnreadable as e:
            raise SystemExit(str(e))
        proto_path = str(Path(a.protocol).expanduser().resolve())
        if not ws.exists:
            _check_adoption(info, a.batch, proto_path)
            ws.adopted = True
            ws.settings = {"schema": wslib.SCHEMA, "name": None, "created": wslib.now(), "updated": wslib.now(),
                           "protocol_path": proto_path, "bom_path": None,
                           "ecad": {"program": a.ecad or os.environ.get("POLYBENCH_ECAD") or "fusion", "document": None, "documents": {}},
                           "current_batch": a.batch}
            return ws, a.batch, None, None, True
        if ws.get("protocol_path") != proto_path:
            ws.overrides["protocol"] = proto_path
        if a.ecad and a.ecad != ws.ecad_program():
            ws.overrides["ecad"] = a.ecad
        if a.batch and a.batch != ws.get("current_batch"):
            ws.overrides["batch"] = a.batch
        return ws, a.batch, a.ecad, proto_path, False
    if a.data_dir:
        d = Path(a.data_dir).expanduser()
        batch = a.batch
    else:
        last = wslib.recent_last()
        if not last:
            return None, None, None, None, False
        d = Path(last["dir"])
        batch = a.batch or last.get("batch")
    try:
        ws = wslib.Workspace(d)
    except wslib.SettingsUnreadable as e:
        raise SystemExit(str(e))
    if not ws.exists:
        raise SystemExit(f"{d} holds no {wslib.SETTINGS_NAME}; start with --protocol to adopt or create it")
    if a.ecad and a.ecad != ws.ecad_program():
        ws.overrides["ecad"] = a.ecad
    if batch and batch != ws.get("current_batch"):
        ws.overrides["batch"] = batch
    return ws, batch, a.ecad, None, False


def cmd_serve(a) -> int:
    import uvicorn

    from . import workspace as wslib
    from .engine import CmdError
    from .server import create_app, open_workspace_session
    from .ai import guard as _guard
    msg, refuse = _guard.op_token_notice(a.port)
    if msg:
        print(msg, file=sys.stderr)
    if refuse:
        return 2
    ws, batch, ecad_o, proto_o, adopted = resolve_start(a)
    if ws is None:                       # first run: no project yet, the page offers New project, Open project and the demo
        from .server import create_app
        app = create_app(None, simulate=a.simulate, meter=make_meter(a))
        url = f"http://127.0.0.1:{a.port}/"
        print("Polybench DV  no project open: the welcome page offers New project, Open project and a simulated demo")
        print(f"open {url}   (Ctrl+C to stop)")
        if not a.no_browser:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=a.port, log_level="warning", timeout_graceful_shutdown=2)).run()
        return 0
    try:
        if adopted:
            ws.persist = False           # write the settings file only once the batch opened cleanly
        sess = open_workspace_session(ws, batch, a.simulate, a.boards, a.operator or "", ecad_o,
                                      proto_o or (a.protocol if adopted else None))
    except CmdError as e:
        print(f"\n{e}", file=sys.stderr)
        return 2
    proto = sess.proto
    for i in proto.issues:
        print(i, file=sys.stderr)
    if adopted:
        ws.persist = True
        sess.seed_document_from_journal()
        ws.save(name=ws.get("name") or proto.data["design"]["name"], current_batch=sess.batch, adopted=True)
        sess.engine.workspace_event("project_settings", {"change": "adopted", "file": str(ws.file), "batch": sess.batch,
                                                         "protocol_path": ws.get("protocol_path"), "ecad_program": ws.ecad_program()})
    wslib.recent_touch(ws.dir, ws.name, sess.batch)
    app = create_app(sess, simulate=a.simulate, meter=make_meter(a))
    url = f"http://127.0.0.1:{a.port}/"
    print(f"Polybench DV  {proto.data['design']['name']}  batch '{sess.batch}'  {'SIMULATED' if sess.simulate else 'real data'}")
    print(f"project: {ws.dir}   settings: {ws.file}{'  (adopted now)' if adopted else ''}")
    if ws.overrides:
        print("for this run only (flags): " + ", ".join(f"{k}={v}" for k, v in ws.overrides.items()))
    print(f"journal: {sess.journal.path}   ({len(sess.journal.events)} events replayed)")
    for pr in sess.journal.problems:
        print("journal warning:", pr, file=sys.stderr)
    print(f"open {url}   (Ctrl+C to stop; stopping loses nothing, every value is already on disk)")
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=a.port, log_level="warning", timeout_graceful_shutdown=2)
    uvicorn.Server(cfg).run()
    return 0


def make_meter(a):
    """The live multimeter service (meter/service.py), or None when switched off. With --simulate it is a fake meter that replays
    scripted frames; otherwise it looks for the CH9329 cable by USB id and says on the card what is missing."""
    import os
    if getattr(a, "no_meter", False) or os.environ.get("POLYBENCH_METER", "").lower() in ("off", "0", "no"):
        return None
    from .meter.service import MeterService
    return MeterService(lambda: None, simulate=bool(a.simulate))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "selftest":
        from .selftest import run
        return run()
    if argv and argv[0] == "doctor":
        from .doctor import main as doctor_main
        return doctor_main(argv[1:])
    if argv and argv[0] == "aoi-derive":
        import json as _json
        ap2 = argparse.ArgumentParser(prog="polybench aoi-derive", description="derive the AOI layout and protocol from a CAM zip and assembly.yaml")
        ap2.add_argument("--cam-zip", required=True)
        ap2.add_argument("--assembly", required=True, help="the BOM / visual fragment (assembly.yaml)")
        ap2.add_argument("--design", required=True)
        ap2.add_argument("--out-dir", required=True)
        a2 = ap2.parse_args(argv[1:])
        from .aoi.derive import derive
        print(_json.dumps(derive(a2.cam_zip, a2.assembly, a2.design, a2.out_dir), indent=1))
        return 0
    if argv and argv[0] == "example":
        from .example_cmd import main as example_main
        return example_main(argv[1:])
    if argv and argv[0] == "plan":
        return cmd_plan(argv[1:])
    if argv and argv[0] == "plugins":
        from .plugins_cmd import main as plugins_main
        return plugins_main(argv[1:])
    if argv and argv[0] in ("mcp", "ai"):          # AI connector: MCP server, setup text, skill installer (src/polybench/ai/cli.py)
        from .ai.cli import main as ai_main
        return ai_main(argv)
    if argv and argv[0] == "lint":
        if len(argv) < 2:
            print("usage: polybench lint <protocol.pcbv.yaml>")
            return 2
        return cmd_lint(argv[1])
    ap = argparse.ArgumentParser(prog="polybench", description="Polybench DV bench validation")
    ap.add_argument("--protocol", help="top protocol file (.pcbv.yaml); without it the last project is reopened (or --data-dir's settings)")
    ap.add_argument("--batch", help="batch name (default DESIGN_DATE); an existing batch is resumed")
    ap.add_argument("--simulate", action="store_true", help="simulated actions and ECAD; data is labelled SIMULATED")
    ap.add_argument("--port", type=int, default=8750)
    ap.add_argument("--boards", type=int, help="number of boards for a new batch")
    ap.add_argument("--data-dir", help="the project folder that holds the batch folders (default ~/polybench-data with --protocol)")
    ap.add_argument("--ecad", help="ECAD program: fusion (default) or kicad; also selects the scanner client (env POLYBENCH_ECAD)")
    ap.add_argument("--operator", help="operator name recorded with results")
    ap.add_argument("--from-open-ecad", action="store_true", help="create the project from the ECAD document that is open now (KiCad or Fusion; needs --protocol; read only)")
    ap.add_argument("--document", help="with --from-open-ecad: part of the name of the document or schematic to use when several are open")
    ap.add_argument("--no-meter", action="store_true", help="do not start the live multimeter reader (also env POLYBENCH_METER=off)")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    if args.from_open_ecad:
        from .open_ecad import run as open_ecad_run
        code = open_ecad_run(args)
        if code is not None:
            return code
    return cmd_serve(args)


if __name__ == "__main__":
    sys.exit(main())
