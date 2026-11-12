# Writing a plug-in

A plug-in is a Python class registered under an entry point. Polybench discovers it, runs it in a child process when the operator presses its button, and turns what it proposes into proposals on tests.

## The contract

From `polybench.plugin_api`:

```python
@dataclass
class ActionContext:
    board: str                 # board label, e.g. "S03"
    params: dict               # from the protocol action
    workdir: Path              # run folder for evidence files
    simulate: bool
    def propose(self, test_id: str, value: str, unit: str | None = None,
                evidence: str | None = None, raw: str | None = None) -> None: ...
    def propose_table(self, test_id: str, rows: list | None = None, columns: list[str] | None = None,
                      csv: str | None = None, evidence: str | None = None, raw: str | None = None) -> None: ...
    def log(self, text: str, level: str = "info") -> None: ...
    def artifact(self, kind: str, data: dict) -> None: ...   # a record for the journal
    def progress(self, fraction: float, text: str = "") -> None: ...
    def cancelled(self) -> bool: ...
    def ask(self, prompt: str, options: list[str]) -> str: ...   # operator prompt

class Action(Protocol):
    name: str                  # e.g. "mybench.read_temp"
    capabilities: list[str]
    def run(self, ctx: ActionContext) -> None: ...    # raises on failure; never computes verdicts
    def safe_state(self) -> str: ...                 # outputs off / bus released, read back where possible
    def selftest(self) -> bool: ...                  # no hardware

class EcadBridge(Protocol):
    name: str
    def status(self) -> dict: ...                    # {connected, program, document, detail}
    def highlight(self, designators: list[str]) -> dict: ...
    def clear(self) -> None: ...
    def designators(self) -> list[str] | None: ...
```

## A minimal action

```python
# mybench/actions.py
import random, time

class ReadTemp:
    """params: map: {temp: TEST_ID}, port"""
    name = "mybench.read_temp"
    capabilities = ["thermometer"]

    def run(self, ctx):
        test = (ctx.params.get("map") or {}).get("temp")
        if not test:
            ctx.log("no test mapped for 'temp'; nothing proposed", "warn")
            return
        if ctx.simulate:                                  # the simulator twin: realistic, no hardware
            value = 24.0 + random.uniform(-0.3, 0.3)
        else:
            value = read_thermometer(ctx.params.get("port", "/dev/tty.usbserial"), timeout_s=5)
        if ctx.cancelled():
            return
        path = ctx.workdir / f"{ctx.board}_temp_{int(time.time())}.txt"
        path.write_text(f"{value:.3f}\n")
        ctx.propose(test, f"{value:.2f}", "degC", evidence=str(path))

    def safe_state(self):
        return "mybench.read_temp: holds nothing"

    def selftest(self):
        return True
```

## Register it

In your package's `pyproject.toml`:

```toml
[project.entry-points."polybench.actions"]
"mybench.read_temp" = "mybench.actions:ReadTemp"

# an ECAD bridge registers under polybench.ecad instead
```

Install the package into the same environment as Polybench (`pip install -e .`). `polybench doctor` lists it under "action plug-ins"; a plug-in that does not load is a red line.

## Use it in a protocol

```yaml
actions:
  - id: temp
    title: Read board temperature
    plugin: mybench.read_temp
    params: {map: {temp: C10}, port: /dev/tty.usbserial-1}
    proposes: [C10]
```

## How it runs

- Each run is a child process: `python -m polybench.run_action <plugin> <json-args>`. The child writes JSON lines on standard output: `proposal`, `log`, `progress`, `ask`, `done`, `error`. The core answers an `ask` on standard input.
- **Abort** sends `{"t": "cancel"}`, waits for the child to finish (there is no kill timer: your plug-in owns its time-outs), then calls `safe_state()` in a fresh child with no params, and reports its return text.
- An exception in `run()` is shown as ERROR in the console with its message. Write messages a person at the bench can act on: "No programmer found: check the USB cable", not a stack trace.
- `params` may contain a `prompt`: the core asks the operator to confirm it before the child starts. Use `ctx.ask()` for questions during the run.

## Checklist

- [ ] `run()` delivers readings only through `ctx.propose()` (or `ctx.propose_table()` for a `kind: table` test) and only to tests named in `params`. The core records them as the results of those tests unless the protocol sets `record: propose`, so deliver only what you measured.
- [ ] No verdicts in the plug-in.
- [ ] Every blocking call has a time-out; `ctx.cancelled()` is checked between steps.
- [ ] Hardware is left safe on success, error and cancel; `safe_state()` works with no params.
- [ ] `ctx.simulate` gives realistic values with no hardware and no network.
- [ ] `selftest()` runs without hardware.
- [ ] Evidence files go to `ctx.workdir`.
