# AOI diff mode: verification

Status: 17 Oct 2026, branch `aoi-diff` (base main `5dd216e` plus `demo-esp32` `c8a2f81`). Everything is HOST-ONLY or UNVALIDATED; there is no hardware in this work and no known-fault board. The mode is **experimental and opt-in** (`aoi: {mode: diff}`); the layout-based AOI is unchanged. Method and limits: `docs/AOI.md`, section "Diff mode (no layout), EXPERIMENTAL".

Scope change (coordinator, 17 Oct): lean build. No schematic study, no part identification; no 3+ board odd-one-out (the design leaves room, see DIFF-04); the operator crop is a protocol parameter, not a UI tool; devkitc-1 variant left as it was.

| ID | Status | Evidence | Label |
|---|---|---|---|
| DIFF-01 layout-free mode, one reference, others registered and compared | met | `aoi: {mode: diff, reference: D01}`; `diff.py`, `diffrun.py`; `tests/aoi/test_aoi_diff.py::test_diff_run_writes_the_result_the_review_reads`; the walk | HOST-ONLY |
| DIFF-02 registration: features + homography + ECC; outline; quality; refuse | partly | SIFT + RANSAC + guided refit + ECC + clipped flow (`diff.register`); inliers, ratio, reprojection RMS reported in the console and `aoi_run.registration`; refusal "retake the photo: ..." tested (`test_poor_registration_is_refused_with_a_retake_message`, noise and featureless). Outline is automatic (edge density) and can be replaced by `crop:` in the protocol; there is **no UI crop tool** (scope change). Synthetic warps recovered with the planted difference as a top-2 candidate | HOST-ONLY |
| DIFF-03 any mask colour; illumination; glare; structure; parallax | met (parallax: reported as a limit only) | no hue is read anywhere in `diff.py`; `test_mask_colour_does_not_matter[red, green, blue, black]`; CLAHE + local-mean removal + SSIM structure term; saturated pixels masked and the fraction reported (`test_glare_is_masked_and_reported_not_compared`); parallax not corrected, stated in the candidate note and in `docs/AOI.md` | HOST-ONLY |
| DIFF-04 ranked candidates, crop pairs, location; decision Y/N/U and which board; odd-one-out; wording | partly | up to 12 ranked regions, each with a sheet (reference crop, board crop, difference map, locator) (screenshots `yd/*_4_review_first.png`, crops `yd/*_crop_*.jpg`); Y needs a named board (`issue_board`), refused otherwise (walk); wording "differs from the reference board", no "defect" in the diff texts (test). **3+ board odd-one-out not built** (scope change): each item carries `diff.box_ref_px`, `diff.boards`, `diff.suggestion: null`, the data a peer comparison needs | HOST-ONLY |
| DIFF-05 plugs into Visual step, journal, gate, report | met | same tiles, photo assignment, review page and checklist; `mode: diff` on `aoi_photo`, `aoi_run`, `aoi_decision` (+ `issue_board`) checked in the walk; undecided blocks, confirmed needs Reworked, the issue follows the named board (walk: issue on D02 then on D01); report section labelled EXPERIMENTAL (`report.py`; `tests/test_aoi_polybench.py::test_diff_mode_decision_names_the_board_gate_follows_it_and_report_says_experimental`; not generated in the browser walk). Experimental badge in the AOI bar and on the review page (screenshots) | HOST-ONLY |
| DIFF-06 strip all EXIF incl. GPS, import and demo | met | `privacy.py`; `tests/aoi/test_aoi_privacy.py`: fixture with GPS, lossless JPEG clean (same pixels), rotated JPEG, HEIC to PNG (same pixels), PNG, non-image untouched, and the `/api/aoi/photos` route stores a clean file; the walk checks stored files; the committed demo photos checked by `tests/test_demo_variants.py`. Non-image bytes are stored as received (no EXIF container to strip) | HOST-ONLY |
| DIFF-07 demo retarget: two variants by one parameter | met, with TBRs | folder `examples/esp32-s3-board/`, variants chosen by the protocol file (`yd-esp32-s3.pcbv.yaml` default, `devkitc-1.pcbv.yaml` kept); GPIO48 sent at run time (`led 48`); maker's schematic linked not copied; mischianti linked not copied. **UART-port silkscreen label not legible in the photos: left TBR with a how-to-tell instruction.** Board facts not researched (scope change). The existing DevKitC-1 walk was not re-run | HOST-ONLY (real board path UNVALIDATED) |
| DIFF-08 demo AOI data and walkthrough | met, result mixed | `aoi/D01_top.jpg`, `D02_top.jpg` (2000 px long side, EXIF stripped), `aoi/README.md`; the walk runs the diff on them. Candidates cover the USB-C/CH343P, BOOT/RST/regulator and RGB passive areas where residue is visible, but the ranking does not separate residue from lighting and angle, and the USB-C shell and module can are listed (parallax, glare). README walkthrough written from the screens | UNVALIDATED |
| DIFF-09 docs with labels | met | `docs/AOI.md` Diff mode section (what it cannot do, labels) | n/a |

## TBR choices

| Value | Choice | Why |
|---|---|---|
| Working size | 1600 px long side | fast, enough for SMD passives at phone distance |
| Registration limits | 40 inliers, ratio 25 %, RMS 3 px, cover 8 % | good pairs give hundreds of inliers and RMS about 1 px; synthetic noise and flat images fail every one |
| Difference threshold | 0.62 (protocol `threshold:` overrides) | set on the demo pair and one synthetic board; not tuned further, so treat as a guess |
| Max candidate area | 2 % of the frame, split at higher levels | a crop of a whole half board is not reviewable |
| Max candidates | 12 | a review list an operator will read |
| Flow residue limit | 6 px | absorbs lens distortion, cannot explain away a real part difference |
| Photo size | 2000 px long side JPEG q90 | 0.6 to 0.7 MB each; enough for the walk |

## Not done

- UI tool to set the crop; crop is a protocol parameter.
- 3+ board odd-one-out suggestion.
- Which USB-C port is UART (TBR); board schematic not read; no part identification.
- The report is tested as markdown only, not generated in the browser; the DevKitC-1 walk (`tests/e2e/esp32_demo_walk.py`) not re-run after the folder rename (only the pytest that loads its protocol).
- Any real-fault validation: no detection rate, no false-alarm rate.
- Pre-existing core layout issue seen: the Visual step shell overflows the window by 19 px at 1100 px wide with or without AOI (measured on the DevKitC-1 variant).
