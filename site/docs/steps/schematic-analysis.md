# Step 2: Schematic analysis <span class="badge planned">Planned</span>

**Why:** many bring-up faults are visible in the schematic before a board exists: a floating enable pin, a part rated below its rail, a net with no test access. Finding them at the desk is cheaper than finding them at the bench.

!!! warning "Not built yet"
    This step is not in the current release. In the app its timeline node is greyed and reads "later". This page describes the intended scope from the product specification; details may change.

## Intended scope

The step will run checks on an imported snapshot of the design and present the results as a queue of findings, one at a time, to accept, reject with a reason, or defer. Each check carries one of three labels:

| Label | Meaning | Who decides |
|---|---|---|
| AUTO | Deterministic code on the design data. Same input, same output. | The code; you read the result. |
| LLM | A local language model reads datasheet text and proposes a value or a finding. | You accept or reject each item. |
| MANUAL | A checklist item for a person. | You. |

No model output would enter the record without a cited source (datasheet file and page, with the quoted text) and your acceptance. The model runs locally by default.

Checks under consideration include netlist integrity, a rail inventory, part ratings against the operating conditions, pin and strap checks, polarity data, the protection concept, test-access gaps, a first-order power budget and assembly data.

Accepted findings will feed the errata register for the design revision and the [test plan](test-plan.md).

## Today

Use the [Project](project.md) step to link the design and check it against the BOM, and record design observations as notes with the category **Design issue**. They are exported as errata candidates from the [Report](report.md) step.
