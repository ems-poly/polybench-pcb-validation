"""Image regression on generated DEMO label images (tests/assembly_scanner/demo_fixtures.py): each image must decode to its
Mouser P/N and resolve against its BOM the way the table below says.

AC-04 (detect identifier) at the image level, AC-05/06 through the resolver, RISK-2 proxy at 1080p.
The images are rendered, not photographed, so passing here does NOT close RISK-2 (a real webcam adds blur, noise and
perspective); the bench step B-3 does.
"""
from __future__ import annotations

import time

import pytest

from asc_support import load_image_bgr, need
import demo_fixtures as D

# image name -> (Mouser P/N on the label, the basket's resolution of it, designators)
LABELS = {
    D.LABEL_A_NAME: (D.PN_R2K2, "MATCH", ["R4", "R5"]),
    D.LABEL_B_NAME: (D.PN_XCVR, "MATCH", ["IC2"]),
}
_cache: dict = {}


def image(demo_files, name, max_side):
    key = (name, max_side)
    if key not in _cache:
        _cache[key] = load_image_bgr(demo_files[name], max_side)
    return _cache[key]


def test_fixture_set_present(demo_files):
    assert all(demo_files[n].is_file() and demo_files[n].stat().st_size > 5000 for n in LABELS)


# 1920 = long side of a 1080p webcam frame: a crude proxy for RISK-2.
@pytest.mark.parametrize("max_side", [None, 1920], ids=["full", "1080p"])
@pytest.mark.parametrize("name", list(LABELS))
def test_label_decodes_to_expected_pn(demo_files, name, max_side):
    scanner = need("polybench.assembly_scanner.scanner")
    codes = scanner.decode(image(demo_files, name, max_side))
    assert isinstance(codes, list) and all(isinstance(c, str) for c in codes)
    from polybench.assembly_scanner.models import normalize_pn
    assert normalize_pn(LABELS[name][0]) in {normalize_pn(c) for c in codes}, codes


@pytest.mark.parametrize("name", list(LABELS))
def test_label_resolves_against_its_bom(demo_files, bom_a, name):
    pn, status, designators = LABELS[name]
    scanner, resolver = need("polybench.assembly_scanner.scanner"), need("polybench.assembly_scanner.resolver")
    r = resolver.resolve(scanner.decode(image(demo_files, name, None)), bom_a)
    assert r.status.value == status
    assert r.mouser_pn == pn
    assert sorted(r.designators) == sorted(designators)


def test_decode_timing_1080p_recorded(demo_files, record_property):
    """No threshold (brief says only 'feels immediate', TBR). Records ms/frame for the report."""
    scanner = need("polybench.assembly_scanner.scanner")
    a = image(demo_files, D.LABEL_A_NAME, 1920)
    scanner.decode(a)
    t = time.perf_counter()
    for _ in range(5):
        scanner.decode(a)
    ms = (time.perf_counter() - t) / 5 * 1000
    record_property("decode_ms_1080p", round(ms, 1))
    print(f"\nscanner.decode on {a.shape[1]}x{a.shape[0]} DEMO label: {ms:.1f} ms/frame")


def test_ac_user_1_tilted_live_frame_needs_and_gets_rotation_sweep(demo_files):
    """AC-USER-1 regression, on a rendered frame: a 1080p landscape frame with the label tilted 5 deg. Plain zxing at 0 deg
    misses the P/N (the test skips if a newer zxing reads it); scanner.decode must still return it."""
    import zxingcpp
    a = image(demo_files, D.LABEL_B_NAME, None)
    raw = {b.text for b in zxingcpp.read_barcodes(a)}
    if D.PN_XCVR in raw:
        pytest.skip("plain zxing now reads this frame; it no longer exercises the rotation sweep")
    assert D.PN_XCVR in need("polybench.assembly_scanner.scanner").decode(a)
