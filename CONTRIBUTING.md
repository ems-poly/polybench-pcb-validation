# Contributing to Polybench

Polybench is maintained by one person, so this document is deliberately strict. It lets contributions be reviewed quickly and safely.

## Repository layout

Polybench is one Python package today (`polybench-pcb-validation`, command `polybench`) with optional extras. Plug-ins live inside it and register through entry points. Splitting the plug-ins into separate packages is a planned later step; this document will then gain a layout table.

| Path | Contents |
|---|---|
| `src/polybench/` | The app: FastAPI server, web UI (`static/`), engine, journal, protocol and plan parsers, CLI |
| `src/polybench/plugins/` | Action plug-ins (`polybench.actions`) and the Fusion bridge |
| `src/polybench/kicad/` | KiCad bridge (`polybench.ecad`) |
| `src/polybench/plugin_api.py` | The plug-in contract |
| `tests/` | Unit tests (simulator only) and `tests/e2e/` browser click-throughs |
| `examples/esp32-s3-board/` | The shipped example: an ESP32-S3 dev board, simulated with `--simulate` |
| `src/polybench/examples/demo/` | DEMO-1, an internal fixture for `polybench selftest`, the tests and the simulator. It is not a user example |
| `site/` | The user manual (MkDocs) |
| `docs/` | Reference documents |

## Development setup

```bash
git clone <REPO URL> polybench && cd polybench
python3.12 -m venv .venv && . .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest -m "not e2e"        # simulator only; no hardware needed
polybench doctor                     # read-only report of this machine
```

Extras you may need for the parts you touch: `scanner`, `aoi`, `kicad`, `voice`, `camera`. The browser tests under `tests/e2e/` need `python -m playwright install webkit chromium`.

CI runs on the simulator only, on macOS and Linux. It never touches real instruments, and there is no self-hosted runner. Hardware validation is a manual step by the maintainer, recorded in the release checklist and labelled in the documentation.

## Verification labels

Say in the pull request what was actually run:

- `HOST-ONLY`: passed on a computer with a simulator. Closes nothing about hardware.
- `HARDWARE-VALIDATED`: the plug-in did what it should against the real instrument. Say which instrument and what you observed.
- `UNVALIDATED`: not run against hardware. That is acceptable for a first contribution if it is stated.

## Plug-in rules

Every plug-in must follow these rules. The automated and the human review check all seven. A pull request that breaks a rule is not merged.

**R1. Simulator path.** Every action works with `ctx.simulate` true and needs no hardware. All plug-in tests run against it. The simulator models the instrument's errors and limits, not only the success path.

**R2. `selftest()`.** Non-destructive, bounded in time, needs no hardware, switches on no output. It reports plug-in health, not the board's.

**R3. `safe_state()`.** Returns every output the plug-in controls to a safe condition: supplies off, loads off, relays open, buses released. It must be idempotent, must not raise, and must work at any time (after errors, timeouts and mid-measurement). The core calls it on abort, on error and after a crash, in a fresh child process with no params.

**R4. No verdicts in plug-ins.** A plug-in proposes values with units and evidence (`ctx.propose`). It never decides pass or fail. Limits live in the protocol and are evaluated by the core only.

**R5. No network calls of its own.** No telemetry, update checks, downloads or cloud APIs, at import or at run time. A LAN instrument is reached only at the address the operator configured; an ECAD bridge only through the local interface of the program it drives.

**R6. Child-process isolation.** Actions run in a child process started by the core and talk to it through `plugin_api.py` only. Do not assume shared memory or globals, leave no threads or processes running after a call returns, and tolerate being killed. Start no process you cannot also stop, and never stop one you did not start.

**R7. One controller per instrument.** For a session, one plug-in instance owns an instrument's connection. A second attempt to open an instrument that is already held must fail with a clear error and must not take the connection over.

## Pull requests

- One change per pull request, with tests on the simulator. For a plug-in, include the `selftest` result and, if you have the instrument, a hardware report (issue form).
- Do not commit client data, board files, photographs, captures, instrument serial numbers, IP addresses of your lab or absolute paths from your machine. Look at every screenshot you add; screenshots show more than you intend.
- Keep dependencies minimal. A new runtime dependency needs a reason in the pull request, and its licence must be compatible with MIT (no GPL or AGPL in the core; an optional extra that is GPL needs a discussion first).
- Do not change `.github/` in a pull request from a fork; the maintainer does that.
- The maintainer merges. Nothing is merged automatically.

## AI-assisted support on this repository

This repository uses Claude (Anthropic) through GitHub Actions. All of its output is advisory. A human reviews and merges every change.

| What | Who can trigger it | What happens |
|---|---|---|
| New issue | Anyone | Labels are suggested and applied only from the existing label list. If `polybench doctor --json` output is missing, a request is posted once. |
| Label `claude:investigate` | Maintainer | An analysis comment is posted. Claude reads the code; it does not run hardware. |
| `@claude fix` comment on an issue | Collaborators with write access | Claude commits to a `claude/` branch and a draft pull request is opened. |
| Pull request opened or updated (branches in this repository) | Automatic | A comment-only review against the plug-in rules is posted. |
| `@claude review` comment on a pull request | Collaborators with write access | The same review for pull requests from forks. Fork code is read as a diff, never run. |
| Weekly | Schedule | A digest issue is posted. |

Issue and pull request text is sent to Anthropic's API when these workflows run. Do not paste anything into an issue that you would not want a cloud model to read. Automated comments may be wrong; say so in the thread. Do not try to instruct the bot through issue or pull request text; it is told to treat that text as data.

## Licence

By contributing, you agree that your contribution is licensed under the repository's licence (MIT).
