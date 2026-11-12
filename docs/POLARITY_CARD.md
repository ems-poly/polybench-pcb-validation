# Polarity card (Assembly aid)

For a polarised BOM line the Assembly aid shows a card under the camera with three drawings and one text line: the schematic symbol, the part from above with its body marking, and the footprint on the board at its real rotation and side with the polarity pad highlighted. It is drawn only from design data.

## Chain, and what makes a card amber

1. **Footprint:** `polybench.footprints.read_footprints` reads the placed element, its package pads, the package silkscreen and Fusion's own placed pads from the active Electronics board (read-only tools only; nothing is selected, activated or edited). The library pad is rotated with the element's angle and side and must land within 0.03 mm of Fusion's own placed pad of the same name. That proves rotation, mirror and side.
2. **Marking:** the BOM line carries `polmark:` next to the free-text `polarity:`.
   `symbol` (diode, tvs, led, led_rgb_ca, ic, crystal), `family`, `marking` (band, pin1_dot, triangle, chamfer_underside), `terminal` (cathode, anode, pin1, pad1), then how the pad is found: `net` (the cathode goes to VIN: exactly one pad on that net) or `pad`; optional `nets` (pad to net expectations that tie the library pad numbers to the datasheet pin map), `legend`, `corner`, `harmless_180`, `caution`, `ref` (datasheet and page).
3. **Checks:** every pad verified; polarity pad found uniquely; `nets` hold; for a pin-1 mark the numbered pads run counter-clockwise from pad 1; a silkscreen circle or arc in the footprint sits beside the polarity pad.

If any link is missing or disagrees, the card shows the symbol and the generic body marking and, in place of the board panel, **"Orientation not confirmed from design data, check the silkscreen"** with the reason. A line without `polmark` is amber too.

## Cache and ECAD off

Reads are memoised for 45 s and every good read is written to `<batch>/polarity_cache.json` per document. If Fusion cannot be read the cached read is used and the source line says `[cached read]`. With ECAD off (step 1) and no cache the card is amber.

## Endpoint and files

`GET /api/polarity?line=N&d=DESIGNATOR`, `src/polybench/polarity_card.py` (decision and geometry), `src/polybench/footprints.py` (Fusion read), `static/polarity.js` (drawing). Tests: `tests/test_polarity_card.py`, `tests/e2e/polarity_card.py`. The simulated ECAD replays a recorded read of a demonstration board (`assembly_scanner/sim_demo_reads.json`).
