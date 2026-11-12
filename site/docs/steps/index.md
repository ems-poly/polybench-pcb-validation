# The twelve steps

Polybench splits prototype validation into twelve steps in two phases. Each page below says why the step exists, how to do it, the keys that matter and what is recorded.

| # | Step | Phase | Status | In one line |
|---|---|---|---|---|
| 1 | [Project](project.md) | Design | <span class="badge ready">Ready</span> | Link the ECAD document and cross-check it against the BOM. |
| 2 | [Schematic analysis](schematic-analysis.md) | Design | <span class="badge planned">Planned</span> | Automated and assisted checks of the schematic. |
| 3 | [BOM and manufacturing](bom-and-manufacturing.md) | Design | <span class="badge planned">Planned</span> | BOM review: ratings, lifecycle, substitutes, manufacturing data. |
| 4 | [Test plan](test-plan.md) | Design | <span class="badge preview">Preview</span> | The markdown test plan, the tests generated from it and its coverage. |
| 5 | [Batch setup](batch-setup.md) | Bench | <span class="badge ready">Ready</span> | Operator, board count and serials. |
| 6 | [Assembly aid](assembly-aid.md) | Bench | <span class="badge ready">Ready</span> | Scan a part bag, see where it goes, count placements across the batch. |
| 7 | [Visual and AOI](visual-aoi.md) | Bench | <span class="badge ready">Ready</span> | Visual checklist; photos, AOI run and issue review. |
| 8 | [Unpowered testing](unpowered-testing.md) | Bench | <span class="badge ready">Ready</span> | Resistance and continuity before first power; tiers and the decision gate (Preview). |
| 9 | [Powered testing](powered-testing.md) | Bench | <span class="badge ready">Ready</span> | First power, rails and current. |
| 10 | [Program and function](program-and-function.md) | Bench | <span class="badge ready">Ready</span> | Flash, identify and test every function the board declares. |
| 11 | [Characterisation](characterisation.md) | Bench | <span class="badge ready">Ready</span> | Guided captures reduced to statistics. |
| 12 | [Report](report.md) | Bench | <span class="badge ready">Ready</span> | Markdown and HTML report, errata candidates. |

"Ready" means the step is built and covered by the automated tests. Whether an individual instrument plug-in has run on real hardware is listed in [Plug-ins and instruments](../guides/plugins.md).

## Rules shared by the bench steps

- **Step-first.** You do one step across all boards, then the next. The board strip shows every board's state in the current step.
- **Gates.** A step can require earlier steps to be complete without a FAIL for that board. The focus panel says **Gated** and why; **Override with a reason...** records the override in the journal and the report.
- **Settled.** A cell is settled when it holds an accepted value or a skip with a reason. Skipped and not-run cells are never shown as passed.
- **FAIL needs its own key.** Enter never records a FAIL. Use **Record FAIL** or Shift+Enter.
- **Retest, not overwrite.** **Send S0n back for retest...** opens a new attempt with a cause and fix; the first attempt stays in the record.
- **Notes everywhere.** Press `c` to add a note linked to what you are looking at. See [Journal and notes](../guides/journal-and-notes.md).

## Which steps a protocol uses

A protocol declares the bench steps it needs by `id`: `assembly`, `visual`, `unpowered`, `powered`, `function`, `characterisation`. Batch setup and Report are always added, and steps 1 and 4 are always shown. See [Protocol file](../reference/protocol.md).
