# Glossary

**Action**
: A Run button in a protocol section. It calls a plug-in on the current board and proposes values to tests.

**AOI**
: Automated optical inspection. Lists candidate defects from board photos for the operator to decide. See [Visual and AOI](steps/visual-aoi.md).

**Attempt**
: The n-th time a board runs a step. Attempt 2 and later are retests.

**Batch**
: A set of boards built and tested together against one protocol file.

**Board label**
: The protocol's prefix plus a number, for example `S03`.

**Capability**
: A kind of instrument the protocol needs, such as a programmer or a DC source.

**Cell**
: One test on one board for one attempt. Holds at most one accepted result.

**Closes on**
: What measurement or decision will turn a guess or tbr limit into a real one.

**Coverage**
: In step 4, which BOM designators the generated tests cover and which they do not.

**Decision gate**
: Rules in the test plan that propose which boards go to Tier 2 after Tier 1.

**DNF**
: Do not fit. A BOM line that is not placed.

**ECAD bridge**
: The plug-in that reads the board from the ECAD program and highlights parts.

**Errata candidates**
: Design issues, FAILs with notes and confirmed AOI defects, exported for the next design revision.

**Gate**
: A requirement that earlier steps are complete without a FAIL before a board enters a step. An override needs a reason.

**Guess limit**
: An estimate. Outside it a value is flagged for review, never failed.

**Hard limit**
: A limit from a datasheet or a design calculation. Outside it a value fails.

**HARDWARE-VALIDATED, HOST-ONLY, UNVALIDATED**
: Readiness labels: results from real boards, from a simulated run, or no results.

**Journal**
: The append-only file of every event of a batch, `journal.jsonl`.

**Outlier**
: A reading far from the batch median, by the band the test plan gives.

**Plan load**
: A record that a test plan with a given hash was loaded and its tests generated.

**Polarity card**
: The Assembly aid's three-panel drawing of a polarised part's orientation, drawn from design data.

**Power interlock**
: With `hold power`, a board with an out-of-band unpowered reading cannot be powered until Tier 2 is done and Tier 1 repeated.

**Probe card**
: The panel that shows where to put the meter leads, and highlights the parts in the ECAD program.

**Proposal**
: A value offered to a cell by voice, the AI assistant or an LLM. Not a result until you accept it. (An action can also be set to `record: propose`.)

**Instrument result**
: A reading delivered by an action. It is recorded as the result of its cell as soon as it arrives, whatever its verdict. You move on, or press **Redo**.

**Anomaly**
: A recorded result that is REVIEW, FAIL, outside a guess limit or that tripped a stop rule. Listed at the top of the step. It informs and never blocks you.

**Table result**
: A result with columns and rows (for example a load sweep). Each row has a verdict; the cell shows the worst.

**Protocol file**
: The `.pcbv.yaml` description of a board design for Polybench.

**Record limit**
: Records the value with no verdict.

**Retest**
: Sending a board back to a step with a cause and fix. Opens a new attempt; earlier attempts stay.

**Retired test**
: A test a newer plan revision dropped. Its results stay in the journal and the report.

**Row mode**
: Entering one test across all boards before the next test.

**Safe state**
: What a plug-in does to leave its instrument safe: outputs off, bus released.

**SIMULATED**
: The label on a batch run with `--simulate`. Nothing in it was measured.

**Stop rule**
: A threshold on a test that blocks the board when crossed, with a message to the operator.

**TBR limit**
: To be reviewed. Gives no verdict and is listed as open.

**Test plan**
: The markdown document that is the master for the Unpowered and Powered tests. See [Test plan format](reference/plan-format.md).

**Tier 1, Tier 2**
: Tier 1 tests run on every board; Tier 2 deeper review runs only on boards the decision gate or the operator sends there.
