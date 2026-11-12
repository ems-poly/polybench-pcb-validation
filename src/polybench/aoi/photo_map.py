"""Map photo files to board ids explicitly. Alphabetical order is never used.

Two accepted sources, and nothing else:
  * a mapping file (JSON): {"B1": "IMG_1.HEIC", "B2": {"top": "a.heic", "bottom": "b.heic"}, ...}
    relative paths are resolved against the photo folder;
  * filenames that carry the board id as a token: B1_top.heic, B3-bottom.jpg, board2.png.
    A name with no side token is a top photo.
Any photo file that cannot be assigned is an error, listed by name.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PHOTO_SUFFIXES = {'.heic', '.jpg', '.jpeg', '.png'}
BOARD = re.compile(r'(?<![A-Za-z0-9])(?:B|board[_ -]?)(\d{1,2})(?![0-9])', re.I)
SIDE = re.compile(r'(?<![A-Za-z])(top|bottom|bot)(?![A-Za-z])', re.I)


class PhotoMapError(ValueError):
    pass


def board_id(number):
    return f'B{int(number)}'


def _entry(value, base, key):
    if isinstance(value, str):
        value = {'top': value}
    if not isinstance(value, dict) or not set(value) <= {'top', 'bottom'} or 'top' not in value and 'bottom' not in value:
        raise PhotoMapError(f'{key}: give a filename or {{"top": ..., "bottom": ...}}')
    out = {}
    for side, name in value.items():
        path = Path(name)
        path = path if path.is_absolute() else base / path
        if not path.is_file():
            raise PhotoMapError(f'{key} {side}: {path} does not exist')
        out[side] = path.resolve()
    return out


def from_map_file(map_file, photos_dir):
    try:
        data = json.loads(Path(map_file).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise PhotoMapError(f'Cannot read map file {map_file}: {error}') from error
    if not isinstance(data, dict) or not data:
        raise PhotoMapError('The map file must be a JSON object keyed by board id.')
    mapping = {}
    for key, value in data.items():
        if not re.fullmatch(r'B\d{1,2}', key):
            raise PhotoMapError(f'Map key {key!r} is not a board id such as B1.')
        mapping[key] = _entry(value, Path(photos_dir), key)
    used = [p for sides in mapping.values() for p in sides.values()]
    if len(set(used)) != len(used):
        raise PhotoMapError('The same photo is mapped to more than one board or side.')
    return mapping


def from_filenames(photos_dir):
    photos = sorted(p for p in Path(photos_dir).iterdir()
                    if p.is_file() and p.suffix.lower() in PHOTO_SUFFIXES and not p.name.startswith('.'))
    mapping, unmapped = {}, []
    for path in photos:
        board = BOARD.search(path.stem)
        if not board:
            unmapped.append(path.name)
            continue
        side = SIDE.search(path.stem)
        side = 'top' if not side else ('top' if side.group(1).lower() == 'top' else 'bottom')
        slot = mapping.setdefault(board_id(board.group(1)), {})
        if side in slot:
            raise PhotoMapError(f'{board_id(board.group(1))} {side} matches two files: {slot[side].name} and {path.name}')
        slot[side] = path.resolve()
    if unmapped:
        raise PhotoMapError('These photos carry no board id (B1..B5) in the name: ' + ', '.join(unmapped)
                            + '. Rename them (B1_top.heic) or pass --map with an explicit mapping file.')
    if not mapping:
        raise PhotoMapError(f'No photos found in {photos_dir}.')
    return mapping


def build(photos_dir, map_file=None):
    photos_dir = Path(photos_dir)
    if not photos_dir.is_dir():
        raise PhotoMapError(f'Photo folder {photos_dir} does not exist.')
    return from_map_file(map_file, photos_dir) if map_file else from_filenames(photos_dir)


def caption_boards(photos_dir):
    """Board ids with a caption line in captions.txt / captions.md (a line starting with B<n>)."""
    for name in ('captions.txt', 'captions.md'):
        path = Path(photos_dir) / name
        if path.is_file():
            found = set()
            for line in path.read_text(errors='replace').splitlines():
                match = re.match(r'\s*[-*]?\s*(?:B|board[_ -]?)(\d{1,2})(?![0-9])\s*[:,\- ]*\S', line, re.I)
                if match:
                    found.add(board_id(match.group(1)))
            return found
    return set()
