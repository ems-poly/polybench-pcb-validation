"""Status logic for the protocol run: pure functions, no images, no Fusion.

Inputs are measurements per designator (from aoi.vision / aoi.batch) and the protocol data.
Rules, in short:
  * `pass` is only ever returned for the presence/absence of a LARGE-class part that the
    exposed-solder-mask test establishes, for a presence row whose ceiling allows it, and for a
    file check. Nothing optical at joint, marking, polarity or small-part level can pass.
  * `fail` is only returned for a large part whose measured presence contradicts the expectation
    (U1 absent on boards 1 to 4, U1 present on board 5, IC4 absent, ...). It is a prompt to look,
    not a verdict.
  * `review` means a person should look at the evidence crop. `not_assessable` means the method
    has nothing to say (class too small, region unusable, photo or side missing).
  * Every status is clamped to the row ceiling in the protocol.
"""
from __future__ import annotations

import re

from polybench.aoi.protocol import RANK, class_of

CONF_ESTABLISHED = 0.85   # large-part presence measured, region usable. Coarse, not a calibrated probability.
CONF_REVIEW = 0.5
CONF_WEAK = 0.3
SHORT = 70


def natural_key(name):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', name)]


def clamp(status, ceiling, fail_allowed=False):
    """Never return more than the ceiling. `fail` survives only where the caller says the
    evidence is an absolute contradiction (large part)."""
    if ceiling == 'not_assessable':
        return 'not_assessable'
    if status == 'fail':
        return 'fail' if fail_allowed else 'review'
    if status == 'pass' and RANK[ceiling] < RANK['pass']:
        return 'review'
    return status


def part_state(measure, thresholds):
    """present / absent / ambiguous from the exposed-solder-mask fraction; unusable if no measure."""
    if not measure or not measure.get('usable') or measure.get('exposure') is None:
        return 'unusable'
    exposure = measure['exposure']
    if exposure <= thresholds['present_max_exposure']:
        return 'present'
    if exposure >= thresholds['absent_min_exposure']:
        return 'absent'
    return 'ambiguous'


def expectation(designator, board, protocol):
    if designator in protocol['boards'][board]['expect_absent']:
        return 'absent'
    if designator in protocol['dnf']:
        return 'absent'
    if designator in protocol['optional']:
        return 'record'
    return 'present'


def watch_set(protocol):
    return set(protocol['dnf']) | set(protocol['optional']) | set(protocol['watch_fitted'])


def unavailable(side, ctx):
    if not ctx['photos'].get(side):
        return f'no {side} photo supplied'
    error = ctx.get('side_errors', {}).get(side)
    if error:
        return f'{side} photo not registered to the layout ({error})'
    return None


def _short(text):
    text = ' '.join(str(text).split())
    return text if len(text) <= SHORT else text[:SHORT - 1] + '…'


def evaluate_part(designator, ctx, protocol, parts):
    cls = class_of(protocol, designator)
    expected = expectation(designator, ctx['board'], protocol)
    out = dict(designator=designator, cls=cls, expected=expected, state='unknown', status='not_assessable',
               value='', confidence=0.0, note='', usable=False, anomaly='')
    info = parts.get(designator)
    if info is None:
        out['note'] = f'{designator} is not in the layout'
        return out
    why = unavailable(info['side'], ctx)
    if why:
        out['note'] = why
        return out
    measure = ctx['measures'].get(designator)
    if not measure or not measure.get('usable'):
        out['note'] = _short(measure.get('reason', 'no region measured')) if measure else 'no region measured'
        return out
    out['usable'] = True
    state = part_state(measure, protocol['thresholds'])
    out['state'] = state
    peer_out = measure.get('peer') in ('outlier', 'disagreement')
    ceiling = protocol['classes'][cls]['ceiling']
    watched = designator in watch_set(protocol)

    if cls == 'large':
        want_present = expected != 'absent'
        if state == 'ambiguous':
            out.update(status='review', confidence=CONF_REVIEW,
                       note='Exposed solder mask is partial in the body region: neither clearly present nor absent.')
        elif (state == 'present') == want_present:
            if want_present and peer_out:
                out.update(status='review', value=f'{designator} present', confidence=CONF_REVIEW,
                           anomaly='appearance differs from the batch median',
                           note='Body present but its appearance differs from the other boards (placement, orientation, '
                                'wrong part, lighting): review the crop.')
            else:
                out.update(status='pass', value=f'{designator} {state}', confidence=CONF_ESTABLISHED,
                           note=('Absent, as this board is expected to be built.' if not want_present else
                                 'Body detected in the footprint; presence only, not value, orientation or solder.'))
        else:
            out.update(status='fail', value=f'{designator} {state}', confidence=CONF_ESTABLISHED,
                       note=(f'{designator} is present where it should be absent for {ctx["board"]}.' if state == 'present'
                             else f'{designator} appears absent: exposed solder mask in the body region.'))
        out['status'] = clamp(out['status'], ceiling, fail_allowed=True)
        return out

    anomalies = []
    if peer_out:
        anomalies.append('appearance differs from the batch median')
    if cls == 'medium' and expected == 'present' and state == 'absent':
        anomalies.append('exposed solder mask in the body region (possible missing part)')
    out['anomaly'] = '; '.join(anomalies)
    if watched or anomalies:
        notes = []
        if expected == 'absent':
            notes.append('Should be absent (DNF); AOI cannot establish absence of a part this small. STOP condition if fitted: check the crop.')
        elif expected == 'record':
            notes.append('Optional part: record by eye whether it is fitted.')
        elif watched:
            notes.append('Must be fitted; AOI cannot establish presence of a part this small. STOP condition if missing: check the crop.')
        if anomalies:
            notes.append(out['anomaly'].capitalize() + '.')
        out.update(status='review', confidence=CONF_REVIEW if anomalies else CONF_WEAK, note=' '.join(notes))
    else:
        out.update(status='not_assessable', confidence=0.0,
                   note=f'Class {cls}: presence is not established by the method; no anomaly against the batch.')
    out['status'] = clamp(out['status'], ceiling)
    return out


def _worst(statuses):
    if 'fail' in statuses:
        return 'fail'
    if statuses and all(s == 'pass' for s in statuses):
        return 'pass'
    if statuses and all(s == 'not_assessable' for s in statuses):
        return 'not_assessable'
    return 'review'


def _result(row, status, value='', confidence=0.0, note='', designators=()):
    ceiling = row['ceiling']
    status = clamp(status, ceiling, fail_allowed=status == 'fail')
    if status == 'not_assessable':
        value, confidence = '', 0.0
    if status != 'pass' and value in ('OK', 'Y'):
        value = ''
    return {'id': row['id'], 'status': status, 'value': value, 'confidence': round(confidence, 2),
            'note': note, '_evidence': list(designators), '_regions': row.get('regions', [])}


def evaluate_row(row, ctx, protocol, parts):
    check = row['check']
    limits = row['limits']
    if check == 'none':
        return _result(row, 'not_assessable', note=limits)
    if check == 'file_check':
        have = [s for s in ('top', 'bottom') if ctx['photos'].get(s)]
        captioned = ctx.get('captioned', False)
        if len(have) == 2 and captioned:
            return _result(row, 'pass', row.get('pass_value', 'Y'), 1.0,
                           'Top and bottom photos are mapped to this board and a caption line exists. ' + limits)
        missing = [f'{s} photo' for s in ('top', 'bottom') if s not in have] + ([] if captioned else ['caption line'])
        return _result(row, 'review' if have else 'not_assessable', '', CONF_REVIEW if have else 0.0,
                       'Missing: ' + ', '.join(missing) + '. ' + limits)
    for side in row['requires']:
        why = unavailable(side, ctx)
        if why:
            return _result(row, 'not_assessable', note=f'{why[0].upper()}{why[1:]}; this row needs the {side} side. {limits}')
    results = [evaluate_part(d, ctx, protocol, parts) for d in row['designators']]
    statuses = [r['status'] for r in results]
    usable = [r for r in results if r['usable']]
    flagged = [f'{r["designator"]}: {r["anomaly"]}' for r in results if r['anomaly']]
    unusable = [f'{r["designator"]} ({r["note"]})' for r in results if not r['usable']]
    notes = []
    for r in results:
        if r['status'] == 'fail':
            notes.append(r['note'])
    if flagged:
        notes.append('Differs from batch: ' + '; '.join(flagged) + '.')
    if unusable:
        notes.append('Not usable: ' + ', '.join(unusable[:6]) + (f' and {len(unusable) - 6} more' if len(unusable) > 6 else '') + '.')
    if check == 'presence':
        status = _worst(statuses)
        value = ''
        if status == 'pass':
            value = row.get('pass_value', 'OK')
        elif status == 'fail':
            value = '; '.join(r['value'] for r in results if r['status'] == 'fail')
        confidence = min([r['confidence'] for r in results], default=0.0)
        if status == 'pass':
            notes.append('Presence only, against the exposed-mask test: not placement, orientation or solder.')
        return _result(row, status, value, confidence, ' '.join(notes + [limits]), row['designators'])
    if check == 'presence_visual':
        r = results[0] if results else None
        if r is None or not r['usable']:
            return _result(row, 'not_assessable', note=' '.join(notes + [limits]))
        if r['status'] == 'fail':
            return _result(row, 'fail', r['value'], r['confidence'], ' '.join(notes + [limits]), row['designators'])
        value = r['value'] if r['state'] in ('present', 'absent') else ''
        note = list(notes)
        if r['state'] == 'absent' and r['expected'] == 'absent':
            note.append(f'{r["designator"]} absent, as expected for {ctx["board"]}; not flagged.')
        return _result(row, 'review', value, CONF_REVIEW, ' '.join(note + [limits]), row['designators'])
    if check == 'polarity_check' and ctx.get('polarity'):
        # ctx['polarity'] = {designator: {'status', 'note'}} from aoi.polarity; pass only when every part passes
        pol = ctx['polarity']
        got = [pol[d]['status'] for d in row['designators'] if d in pol]
        status = _worst(got) if len(got) == len(row['designators']) else ('fail' if 'fail' in got else 'review')
        detail = '; '.join(f'{d} {pol[d]["status"]}' for d in row['designators'] if d in pol)
        notes.append('Polarity against design and datasheet (aoi.polarity): ' + detail + '.')
        return _result(row, status, 'OK' if status == 'pass' else '', CONF_ESTABLISHED if status in ('pass', 'fail') else CONF_REVIEW,
                       ' '.join(notes + [limits]), row['designators'])
    # joint_review, bridge_suspicion, orientation_mark, marking_read, small_parts_review, watch_presence
    if 'fail' in statuses:
        status = 'fail'
    elif usable:
        status = 'review'
    else:
        status = 'not_assessable'
    if check == 'small_parts_review':
        notes.append(f'{len(usable)} of {len(results)} regions usable.')
    confidence = CONF_REVIEW if status == 'review' else (CONF_ESTABLISHED if status == 'fail' else 0.0)
    return _result(row, status, '', confidence, ' '.join(notes + [limits]), row['designators'])


def evaluate_extras(ctx, protocol, parts, include_not_assessable=False):
    """One `AOI-<designator>` result per BOM designator that AOI has something to say about."""
    out = []
    for designator in sorted(parts, key=natural_key):
        if designator in protocol['non_parts']:
            continue
        r = evaluate_part(designator, ctx, protocol, parts)
        if r['status'] == 'not_assessable' and not include_not_assessable:
            continue
        note = r['note']
        if ctx['board'] and designator in protocol['boards'][ctx['board']]['expect_absent']:
            note = f'{ctx["board"]} is built without {designator} by design. ' + note
        out.append({'id': f'AOI-{designator}', 'status': r['status'], 'value': r['value'],
                    'confidence': round(r['confidence'], 2), 'note': note.strip(),
                    '_evidence': [designator] if r['status'] != 'not_assessable' else [], '_regions': []})
    return out


def evaluate_board(protocol, parts, ctx, include_not_assessable=False):
    rows = [evaluate_row(row, ctx, protocol, parts) for row in protocol['rows']]
    return rows + evaluate_extras(ctx, protocol, parts, include_not_assessable)
