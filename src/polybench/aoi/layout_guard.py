"""Refuse to inspect against the wrong layout.

Several copies of `the design` can be open in Fusion. The layout the tool read (live or
recorded) is compared part by part with the pick-and-place files of the released CAM package:
designator set, value, package, side, angle and position. The zip is read in memory with
zipfile; nothing is extracted and nothing in Fusion is touched.
"""
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

POSITION_TOL_MM = 0.02
ANGLE_TOL_DEG = 0.5
# The PnP file gives J1's pick centre, the layout gives its package origin: 1.42 mm apart in x.
# Only the x position of these is exempt; everything else about them is still compared.
PNP_ORIGIN_EXEMPT_X = {'J1'}


class LayoutMismatch(RuntimeError):
    pass


def _parse_pnp(text, side):
    parts = {}
    for line in text.splitlines():
        cells = line.rstrip('\r\n').split('\t')
        if len(cells) < 6:
            continue
        try:
            x, y, angle = float(cells[1]), float(cells[2]), float(cells[3])
        except ValueError:
            continue  # header line
        parts[cells[0]] = {'x': x, 'y': y, 'angle': angle, 'value': cells[4], 'package': cells[5], 'side': side}
    return parts


def read_cam_pnp(zip_path):
    """Return (parts by designator, sha256 of the zip)."""
    zip_path = Path(zip_path)
    if not zip_path.exists():
        raise LayoutMismatch(f'CAM package not found: {zip_path}')
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    parts = {}
    with zipfile.ZipFile(zip_path) as archive:
        members = {Path(n).name: n for n in archive.namelist()}
        for side, suffix in (('top', '_front.txt'), ('bottom', '_back.txt')):
            found = [n for base, n in members.items() if base.startswith('PnP_') and base.endswith(suffix)]
            if len(found) != 1:
                raise LayoutMismatch(f'{zip_path.name}: expected one PnP file ending {suffix}, found {len(found)}')
            parts.update(_parse_pnp(archive.read(found[0]).decode('utf-8', 'replace'), side))
    if not parts:
        raise LayoutMismatch(f'{zip_path.name}: no parts in the PnP files')
    return parts, digest


def compare_to_cam(layout, cam_parts, design=None):
    """List of human-readable differences; empty means the layout matches the CAM package. `design` is the board name the layout must carry
    (the protocol's layout.design); None skips the name check."""
    problems = []
    if design is not None and layout.get('board') != design:
        problems.append(f'board name is {layout.get("board")!r}, expected {design!r}')
    if layout.get('unit') != 'mm':
        problems.append(f'coordinate unit is {layout.get("unit")!r}, expected mm')
    live = {p['name']: p for p in layout.get('parts', [])}
    for name in sorted(set(cam_parts) - set(live)):
        problems.append(f'{name} is in the CAM package but not in the layout')
    for name in sorted(set(live) - set(cam_parts)):
        problems.append(f'{name} is in the layout but not in the CAM package')
    for name in sorted(set(live) & set(cam_parts)):
        part, cam = live[name], cam_parts[name]
        for key in ('value', 'package', 'side'):
            if part.get(key) != cam[key]:
                problems.append(f'{name}: {key} {part.get(key)!r} in layout, {cam[key]!r} in CAM')
        if abs(((part['angle'] - cam['angle'] + 180) % 360) - 180) > ANGLE_TOL_DEG:
            problems.append(f'{name}: angle {part["angle"]} in layout, {cam["angle"]} in CAM')
        if abs(part['y'] - cam['y']) > POSITION_TOL_MM or (
                name not in PNP_ORIGIN_EXEMPT_X and abs(part['x'] - cam['x']) > POSITION_TOL_MM):
            problems.append(f'{name}: position ({part["x"]:.2f}, {part["y"]:.2f}) in layout, '
                            f'({cam["x"]:.2f}, {cam["y"]:.2f}) in CAM')
    return problems


def verify_layout(layout, cam_zip, design=None):
    """Raise LayoutMismatch unless the layout matches the CAM package; return evidence for the run log."""
    cam_parts, digest = read_cam_pnp(cam_zip)
    problems = compare_to_cam(layout, cam_parts, design)
    if problems:
        shown = '; '.join(problems[:8]) + (f'; and {len(problems) - 8} more' if len(problems) > 8 else '')
        raise LayoutMismatch(
            f'The layout does not match {Path(cam_zip).name} ({len(problems)} differences): {shown}. '
            'Activate the correct design in Fusion, or pass the matching recorded layout.')
    return {'cam_zip': str(cam_zip), 'cam_zip_sha256': digest, 'parts_compared': len(cam_parts),
            'layout_fingerprint': layout.get('fingerprint'), 'result': 'match'}
