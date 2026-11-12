"""Coordinates of the Label Studio (LS) labelling images and the board frame, in one place (pure, tested).

Each labelling image is a registered top photo warped to board millimetres (`board_view`): layout orientation,
y up, cropped to the board outline. With `box_mm = [x0, y0, x1, y1]` and `ppmm`:

    pixel (u, v) = ((x - x0) * ppmm, (y1 - y) * ppmm)          x = x0 + u / ppmm,  y = y1 - v / ppmm

LS stores shapes as percentages of the image size (rectangle: top-left corner, width, height, rotation in degrees
clockwise about the top-left corner; polygon: points). Labels are exported in board mm, so they survive a new
registration, a different image resolution and new photos of the same board.
"""
from __future__ import annotations

import math

SCHEMA = 'polystage.aoi_labels/0'


def mm_to_px(meta, x, y):
    x0, _, _, y1 = meta['box_mm']
    return (x - x0) * meta['ppmm'], (y1 - y) * meta['ppmm']


def px_to_mm(meta, u, v):
    x0, _, _, y1 = meta['box_mm']
    return x0 + u / meta['ppmm'], y1 - v / meta['ppmm']


def mm_to_pct(meta, x, y):
    u, v = mm_to_px(meta, x, y)
    w, h = meta['size_px']
    return 100.0 * u / w, 100.0 * v / h


def pct_to_mm(meta, px, py):
    w, h = meta['size_px']
    return px_to_mm(meta, px * w / 100.0, py * h / 100.0)


def rect_mm_to_ls(meta, box_mm):
    """(x0, y0, x1, y1) mm -> LS rectangle value (percent, no rotation)."""
    x0, y0, x1, y1 = box_mm
    (l, t), (r, b) = mm_to_pct(meta, x0, y1), mm_to_pct(meta, x1, y0)
    l, t, r, b = max(0.0, l), max(0.0, t), min(100.0, r), min(100.0, b)
    return {'x': l, 'y': t, 'width': r - l, 'height': b - t, 'rotation': 0}


def rect_ls_to_polygon_mm(meta, value):
    """LS rectangle value -> its four corners in board mm (rotation honoured), in the order TL, TR, BR, BL."""
    w, h = meta['size_px']
    u0, v0 = value['x'] * w / 100.0, value['y'] * h / 100.0
    rw, rh = value['width'] * w / 100.0, value['height'] * h / 100.0
    a = math.radians(value.get('rotation', 0) or 0)
    c, s = math.cos(a), math.sin(a)
    corners = [(0, 0), (rw, 0), (rw, rh), (0, rh)]
    return [px_to_mm(meta, u0 + dx * c - dy * s, v0 + dx * s + dy * c) for dx, dy in corners]


def polygon_ls_to_mm(meta, points):
    return [pct_to_mm(meta, px, py) for px, py in points]


def bbox(poly):
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    return [min(xs), min(ys), max(xs), max(ys)]
