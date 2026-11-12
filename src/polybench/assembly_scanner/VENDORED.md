# Vendored Assembly Scanner

Source: the standalone Assembly Scanner prototype's `app/` folder (the version that worked on the bench). Read-only: nothing in that prototype is touched.

| File here | From | Change |
|---|---|---|
| `models.py`, `bom.py`, `resolver.py`, `scanner.py`, `state.py`, `hud.py`, `camera.py`, `fusion_client.py`, `fusion_scripts/assembly_scanner.py` | same names in `app/` | imports only (`app.x` to `polybench.assembly_scanner.x`) |
| `main.py` | `app/main.py` | Qt `main()` removed; `Controller` and `Event` unchanged; `Settings` keeps its own file under `~/Library/Application Support/Polybench/AssemblyScanner/`, seeded once from the standalone app's file (read-only) |
| `service.py` | `app/ui.py` | the window logic without Qt: ScanWorker, FusionWorker, poll and back-off, auto and manual board choice, recursion breaker, pause, camera open and permission, HUD context. State goes to the page as JSON |
| `sim.py`, `polybench_link.py` | new | recorded-frame camera and in-memory Fusion for tests and demos; batch BOM to the PoC importer; service construction |

Not ported: `ui.py` painting (Qt widgets: replaced by the Assembly aid page), the `.app` launcher, the AOI modules.
To refresh from the PoC: copy the nine files again, re-apply the import rename, diff `service.py` against `ui.py`.

**Local additions after the vendoring (16 Oct, test plan as master).** Re-apply them when refreshing from the PoC:
- `fusion_client.py`: `highlight_targets(parts, pins, nets)`, `resolve_pins`, `board_nets`, `_parts_on`, `_items`, and `net_mode`; `_run` takes an `extra` args dict. The parts path is untouched (`highlight`).
- `fusion_scripts/assembly_scanner.py`: with `mode` `nets` or `pinrefs` the highlight op also selects the schematic `Net` objects or `PinRef` objects (`Net.pinRefs`) named in `nets` and `pinrefs`; it raises `SELECT_REJECTED` when Fusion refuses them, so the client falls back.
- `models.py`: `HighlightResult` gained `pins`, `pins_missing`, `nets`, `nets_missing`, `net_mode`, `note`.
- `service.py`: `select_targets()` (a test's targets, sent as an explicit action, waits for the answer).
- `sim.py`: `SimulatedFusionClient.highlight_targets`, a netlist (`POLYBENCH_SIM_NETLIST`) and a selection log (`POLYBENCH_SIM_HIGHLIGHT_LOG`) for tests.
