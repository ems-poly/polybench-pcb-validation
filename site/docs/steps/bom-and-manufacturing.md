# Step 3: BOM and manufacturing <span class="badge planned">Planned</span>

**Why:** a part that is obsolete, out of stock, under-rated or impossible to hand-solder costs a build. Reviewing the BOM line by line before the order catches it.

!!! warning "Not built yet"
    This step is not in the current release. In the app its timeline node reads "BOM review" and "later". This page describes the intended scope from the product specification; details may change.

## Intended scope

A BOM table with, for each line: lifecycle, stock and lead time (with the date of the lookup), rating margins, substitute proposals with a parameter-by-parameter comparison, package and terminal issues (exposed pads, moisture level, hand-solder suitability) and polarity-marking data for AOI. Supplier lookups would go through an adapter and only when you enable one for the project. A failed lookup would show "unknown", never "active".

## Today

The BOM lives in the protocol file (`bom:` in the assembly fragment). It is used by the [Project](project.md) cross-check, the [Assembly aid](assembly-aid.md), the polarity card and AOI. Optional hand-assembly hints on each line (`height_mm`, `body_mm`) improve the Assembly aid's "small and low first" order. See [Protocol file](../reference/protocol.md#bom).
