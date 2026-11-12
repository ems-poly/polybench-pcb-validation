"""Batch inspection: every board vs reference, then component crops vs batch median."""
from __future__ import annotations

import cv2
import numpy as np

from polybench.aoi.vision import inspect, alignment_matrix


def empty_board(number):
    return {'id': f'Board {number:02d}', 'candidate': None, 'candidate_source': None,
            'candidate_anchors': [], 'candidate_alignment': None, 'aligned': None, 'results': []}


def compare_batch(reference, recipe, boards):
    if len(boards) < 2:
        raise ValueError('Capture at least two boards for comparison.')
    outputs = []
    for board in boards:
        if board['candidate'] is None:
            raise ValueError(f"{board['id']}: capture a photo first.")
        try:
            matrix = alignment_matrix(recipe['layout']['parts'], board['candidate_anchors'], board.get('candidate_alignment'))
        except ValueError as error:
            raise ValueError(f"{board['id']}: {error}") from error
        aligned, rows = inspect(reference, board['candidate'], recipe, matrix)
        outputs.append(dict(board, aligned=aligned, results=rows))
    for index in range(len(outputs[0]['results'])):
        samples = []
        for board in outputs:
            row = board['results'][index]
            row.update(peer_status='not_assessed', peer_reason='Insufficient usable peer regions.')
            # Only use regions that passed the single-photo coverage, size and focus gates.
            if 'difference' not in row:
                continue
            polygon = np.float32(row['polygon'])
            x, y, w, h = cv2.boundingRect(polygon)
            crop = cv2.cvtColor(board['aligned'][y:y+h, x:x+w], cv2.COLOR_BGR2GRAY)
            mask = np.zeros((h, w), np.uint8)
            cv2.fillPoly(mask, [np.int32(polygon-[x, y])], 255)
            mask = cv2.erode(mask, np.ones((3, 3), np.uint8))
            crop = cv2.resize(cv2.GaussianBlur(crop, (3, 3), 0), (96, 96)).astype(float)
            mask = cv2.resize(mask, (96, 96), interpolation=cv2.INTER_NEAREST) > 0
            if not mask.any():
                continue
            crop -= np.median(crop[mask])
            samples.append((board, row, crop, mask))
        if len(samples) < 2:
            continue
        common = np.logical_and.reduce([sample[3] for sample in samples])
        pixels = np.stack([sample[2][common] for sample in samples])
        if len(samples) == 2:
            score = float(np.mean(np.abs(pixels[0]-pixels[1]))/255)
            scores = [score, score]
            threshold = recipe.get('peer_threshold', .06)
        else:
            median = np.median(pixels, axis=0)
            scores = np.mean(np.abs(pixels-median), axis=1)/255
            middle = np.median(scores)
            mad = np.median(np.abs(scores-middle))
            threshold = max(recipe.get('peer_threshold', .06), float(middle + max(.025, 3*1.4826*mad)))
        for (board, row, _, _), score in zip(samples, scores):
            row['peer_difference'] = round(float(score), 4)
            row['peer_boards'] = [sample[0]['id'] for sample in samples if sample[0]['id'] != board['id']]
            anomaly = score > threshold
            row['peer_status'] = ('disagreement' if len(samples) == 2 else 'outlier') if anomaly else 'similar'
            row['peer_reason'] = ('Two boards differ; inspect both to identify the cause.' if len(samples) == 2
                                  else 'Unusual appearance compared with the batch median.') if anomaly else 'No large difference from usable peers.'
            if anomaly:
                row['reference_status'] = row['status']
                row['status'] = 'changed'
                row['reason'] += ' Batch: ' + row['peer_reason']
        # A missing dark body can be easier to distinguish in colour than grayscale.
        exposed = [(board, row) for board, row, _, _ in samples if 'board_exposure' in row]
        if len(exposed) >= 3:
            typical = float(np.median([row['board_exposure'] for _, row in exposed]))
            for board, row in exposed:
                if row['fitted'] and row['board_exposure'] > .6 and typical < .15:
                    row['reference_status'] = row.get('reference_status', row['status'])
                    row['peer_status'] = 'outlier'
                    row['status'] = 'changed'
                    row['peer_reason'] = 'Possible missing component: exposed red solder mask where most peers show a body.'
                    row['reason'] += ' Batch: ' + row['peer_reason']
    return outputs
