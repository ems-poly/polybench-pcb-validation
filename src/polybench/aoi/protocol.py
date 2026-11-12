"""Test-protocol loader for the AOI protocol run.

A protocol is a JSON data file: it names the checks of a report (ids come from the report, never
from this code), the designators each involves, what kind of check AOI can make, and the highest
status AOI may report for it (the ceiling). The loader refuses a protocol that asks a check type
for more than the method can establish, so `pass` cannot be configured into existence.
"""
from __future__ import annotations

import json
from pathlib import Path

SCHEMA = 'polystage.aoi_protocol/0'
STATUSES = ('pass', 'fail', 'review', 'not_assessable')
CEILINGS = ('pass', 'review', 'not_assessable')
RANK = {'not_assessable': 0, 'review': 1, 'pass': 2}
SIDES = ('top', 'bottom')

# Highest ceiling each check type can have. Only a large-part presence test and a file test
# can establish a pass; everything optical at joint or marking level stops at review.
CHECK_MAX = {
    'presence': 'pass',
    'file_check': 'pass',
    'presence_visual': 'review',
    'joint_review': 'review',
    'bridge_suspicion': 'review',
    'orientation_mark': 'review',
    # polarity from the design and datasheet versus the photo (aoi.polarity); a pass needs every part to pass and its detector validated
    'polarity_check': 'pass',
    'marking_read': 'review',
    'small_parts_review': 'review',
    'watch_presence': 'review',
    'none': 'not_assessable',
}


DEFAULT = None   # a default protocol file for tools and tests; the app always passes its own


class ProtocolError(ValueError):
    pass


def load_protocol(path=None):
    path = Path(path or proto_default())
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProtocolError(f'Cannot read protocol {path}: {error}') from error
    return validate_protocol(data, source=str(path))


def class_of(protocol, designator):
    """'large', 'medium' or 'small'. A designator listed twice is rejected at load time."""
    for name in ('large', 'medium'):
        if designator in protocol['classes'][name]['designators']:
            return name
    return 'small'


def validate_protocol(data, source='<memory>'):
    def need(condition, message):
        if not condition:
            raise ProtocolError(f'{source}: {message}')

    need(isinstance(data, dict) and data.get('schema') == SCHEMA, f'schema must be {SCHEMA}')
    for key in ('protocol', 'report_ref', 'layout', 'boards', 'classes', 'thresholds', 'rows'):
        need(key in data, f'missing "{key}"')
    boards = data['boards']
    need(isinstance(boards, dict) and boards, 'boards must be a non-empty object')
    for name, board in boards.items():
        need(isinstance(board.get('expect_absent', []), list), f'{name}: expect_absent must be a list')
        board.setdefault('expect_absent', [])
        board.setdefault('label', '')
    need(data.get('reference_board', next(iter(boards))) in boards, 'reference_board is not a board')
    data.setdefault('reference_board', next(iter(boards)))
    for key in ('dnf', 'optional', 'watch_fitted', 'non_parts'):
        data.setdefault(key, [])
    classes = data['classes']
    for name in ('large', 'medium', 'small'):
        need(name in classes, f'classes.{name} missing')
        need(classes[name].get('ceiling') in CEILINGS, f'classes.{name}.ceiling invalid')
    need(classes['small']['ceiling'] != 'pass', 'class small may not have a pass ceiling')
    need(classes['medium']['ceiling'] != 'pass', 'class medium may not have a pass ceiling')
    seen = {}
    for name in ('large', 'medium'):
        for designator in classes[name]['designators']:
            need(designator not in seen, f'{designator} is in classes {seen.get(designator)} and {name}')
            seen[designator] = name
    thresholds = data['thresholds']
    need(0 <= thresholds.get('present_max_exposure', -1) < thresholds.get('absent_min_exposure', -1) <= 1,
         'thresholds need present_max_exposure < absent_min_exposure')
    ids = set()
    for row in data['rows']:
        rid = row.get('id')
        need(isinstance(rid, str) and rid, 'every row needs an id')
        need(rid not in ids, f'duplicate row id {rid}')
        ids.add(rid)
        need(row.get('check') in CHECK_MAX, f'{rid}: unknown check type {row.get("check")!r}')
        need(row.get('ceiling') in CEILINGS, f'{rid}: ceiling must be one of {CEILINGS}')
        need(RANK[row['ceiling']] <= RANK[CHECK_MAX[row['check']]],
             f'{rid}: check type {row["check"]} cannot reach {row["ceiling"]}; maximum is {CHECK_MAX[row["check"]]}')
        row.setdefault('designators', [])
        row.setdefault('requires', [])
        row.setdefault('sides', row['requires'])
        need(all(side in SIDES for side in row['requires'] + row['sides']), f'{rid}: sides must be top/bottom')
        need(row.get('limits'), f'{rid}: state the limits of the method for this row')
        if row['ceiling'] == 'pass' and row['check'] == 'presence':
            small = [d for d in row['designators'] if d not in classes['large']['designators']]
            need(not small, f'{rid}: a pass ceiling needs large-class parts only; {small} are not large')
    need(ids, 'no rows')
    for name, board in boards.items():
        for designator in board['expect_absent']:
            need(isinstance(designator, str), f'{name}: expect_absent entries are designators')
    return data


def proto_default():
    if DEFAULT is None:
        raise ProtocolError('no protocol given')
    return DEFAULT
