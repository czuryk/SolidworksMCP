"""Numeric comparison of a model silhouette with a reference image; COM-free.

The model silhouette comes from the draft of its shape program or from the
tessellation of the built bodies, the reference silhouette from the image
foreground. Both are aligned by their bounding boxes and compared level by
level, so the answer names where and by how many millimetres the model is too
wide or too narrow.
"""

from __future__ import annotations

import math
import uuid
from pathlib import Path

import numpy as np

from .reference_measure import _load, _number
from .shape_draft import VIEWS, silhouettes, view_mask
from .shape_program import ProgramError


def _extent(row):
    """First and one-past-last foreground column of a mask row, or None."""
    columns = np.flatnonzero(row)
    return None if columns.size == 0 else (int(columns[0]), int(columns[-1]) + 1)


OPPOSITE = {'front': 'back', 'back': 'front', 'right': 'left', 'left': 'right', 'top': 'bottom', 'bottom': 'top'}


class DraftSilhouette:
    """Silhouette predicted from the shape program; cuts are not subtracted."""

    method = ('draft silhouette of the shape program against the filled image foreground; x is measured '
              'from the alignment axis, positive to the right of the image; cuts are not subtracted')
    kind = 'draft'

    def __init__(self, drafted):
        self.drafted = drafted

    def bounds(self, view):
        from shapely.ops import unary_union
        shapes = silhouettes(self.drafted, view)
        if not shapes:
            raise ProgramError('DRAFT_EMPTY', 'The program has no body with a predictable shape')
        return unary_union(list(shapes.values())).bounds          # min_x, min_y, max_x, max_y in mm

    def mask(self, view, frame, size):
        return view_mask(self.drafted, view, frame, size)


def _model_mask(source, view, box, scale, align, size):
    """Model silhouette rasterised in the pixel grid of the reference; returns (mask, frame, bounds)."""
    bounds = source.bounds(view)
    left, top, right, bottom = box
    scale = scale or (bottom - top) / (bounds[3] - bounds[1])
    center = 0. if align == 'origin' else (bounds[0] + bounds[2]) / 2
    frame = {'px_per_mm': scale, 'origin_px': [(left + right) / 2 - center*scale, bottom + bounds[1]*scale]}
    return source.mask(view, frame, size), frame, bounds


def _iou(a, b):
    union = np.logical_or(a, b).sum()
    return float(np.logical_and(a, b).sum() / union) if union else 0.


def _bbox(mask):
    rows, columns = np.flatnonzero(mask.any(axis=1)), np.flatnonzero(mask.any(axis=0))
    if rows.size == 0:
        return None
    return int(columns[0]), int(rows[0]), int(columns[-1]) + 1, int(rows[-1]) + 1


def compare_reference(image_path, drafted, view='front', height_mm=None, levels=24, tolerance_mm=1.5,
                      align='bbox', background=None, threshold=32, directory=None):
    """drafted: the draft components of a program, or a silhouette source with bounds() and mask()."""
    source = DraftSilhouette(drafted) if isinstance(drafted, dict) else drafted
    if view not in VIEWS:
        raise ProgramError('INVALID_VIEWS', 'view is one of ' + ', '.join(VIEWS))
    if isinstance(levels, bool) or not isinstance(levels, int) or not 4 <= levels <= 64:
        raise ProgramError('INVALID_ARGUMENT', 'levels must be an integer from 4 to 64')
    if not _number(tolerance_mm) or tolerance_mm <= 0:
        raise ProgramError('INVALID_ARGUMENT', 'tolerance_mm must be positive')
    if height_mm is not None and (not _number(height_mm) or height_mm <= 0):
        raise ProgramError('INVALID_ARGUMENT', 'height_mm must be positive')
    if align not in {'bbox', 'origin'}:
        raise ProgramError('INVALID_ARGUMENT', "align is 'bbox' or 'origin'")
    if not isinstance(threshold, int) or isinstance(threshold, bool) or not 9 <= threshold <= 246:
        raise ProgramError('INVALID_THRESHOLD', 'threshold must be an integer from 9 to 246')
    from scipy.ndimage import binary_fill_holes
    entry, _ = _load(image_path, background, threshold)
    # Interior highlights that match the background are still inside the object.
    reference = binary_fill_holes(np.asarray(entry['masks'][1]) > 0)
    box = _bbox(reference)
    if box is None:
        raise ProgramError('REFERENCE_EMPTY', 'No foreground differs from the background of the reference image')
    left, top, right, bottom = box
    warnings = []
    if left == 0 or top == 0 or right == reference.shape[1] or bottom == reference.shape[0]:
        warnings.append('REFERENCE_TOUCHES_IMAGE_BORDER')
    if entry['background_uncertain']:
        warnings.append('NONUNIFORM_OR_UNKNOWN_BACKGROUND')
    size = (reference.shape[1], reference.shape[0])
    # Scale: the reference silhouette is height_mm tall; without it, as tall as the model.
    fixed_scale = (bottom - top) / height_mm if height_mm else None
    model, frame, model_bounds = _model_mask(source, view, box, fixed_scale, align, size)
    model_width, model_height = model_bounds[2] - model_bounds[0], model_bounds[3] - model_bounds[1]
    scale = frame['px_per_mm']
    iou = _iou(reference, model)
    # A side reference is easily drawn from the other side: say so instead of reporting noise.
    opposite = None
    other, _, _ = _model_mask(source, OPPOSITE[view], box, fixed_scale, align, size)
    other_iou = _iou(reference, other)
    if other_iou > iou + .03:
        opposite = {'view': OPPOSITE[view], 'iou': round(other_iou, 4)}
        warnings.append('REFERENCE_MATCHES_OPPOSITE_VIEW')
    origin_x, origin_y = frame['origin_px']

    def millimetres(extent):
        return None if extent is None else [round((extent[0] - origin_x) / scale, 2),
                                            round((extent[1] - origin_x) / scale, 2)]
    span = _bbox(np.logical_or(reference, model))

    def deviation_of(ref, mod):
        if ref and mod:
            return max(abs(mod[0] - ref[0]), abs(mod[1] - ref[1]))
        # One silhouette has nothing at this height: a missing or extra part.
        present = ref or mod
        return 0. if present is None else present[1] - present[0]
    # A horizontal edge that is off by less than the tolerance must not fail the level it
    # crosses: the model may match at any height within the tolerance of the level.
    reach = int(tolerance_mm * scale)
    profile, worst = [], []
    for index in range(levels):
        row = int(round(span[3] - (index + .5) * (span[3] - span[1]) / levels))
        row = min(max(row, 0), reference.shape[0] - 1)
        height = round((origin_y - row) / scale, 2)
        ref = millimetres(_extent(reference[row]))
        best = None
        for shift in sorted(range(-reach, reach + 1), key=abs):
            other = row + shift
            if not 0 <= other < model.shape[0]:
                continue
            candidate = millimetres(_extent(model[other]))
            found = deviation_of(ref, candidate)
            if best is None or found < best[0] - 1e-9:
                best = (found, shift, candidate)
            if found <= 1e-9:
                break
        deviation, shift, mod = best
        item = {'y_mm': height, 'reference': ref, 'model': mod}
        if shift:
            item['model_y_mm'] = round((origin_y - (row + shift)) / scale, 2)
        if ref and mod:
            item.update(delta_left=round(mod[0] - ref[0], 2), delta_right=round(mod[1] - ref[1], 2),
                        delta_width=round((mod[1] - mod[0]) - (ref[1] - ref[0]), 2))
        else:
            present = ref or mod
            item['missing'] = None if present is None else ('model' if ref else 'reference')
        item['passed'] = deviation <= tolerance_mm
        if not item['passed']:
            worst.append((deviation, item))
        profile.append(item)
    reference_size = [round((right - left) / scale, 2), round((bottom - top) / scale, 2)]
    model_size = [round(model_width, 2), round(model_height, 2)]
    result = {
        'view': view, 'source': source.kind, 'reference': {'path': str(Path(image_path)), 'sha256': entry['sha256'],
                                    'size_px': [reference.shape[1], reference.shape[0]], 'bbox_px': list(box)},
        'scale_mm_per_px': round(1 / scale, 5),
        'scale_source': 'height_mm' if height_mm else 'model_height',
        'align': align, 'iou': round(iou, 4),
        **({'opposite_view': opposite} if opposite else {}),
        'reference_size_mm': reference_size, 'model_size_mm': model_size,
        'delta_size_mm': [round(model_size[0] - reference_size[0], 2), round(model_size[1] - reference_size[1], 2)],
        'tolerance_mm': tolerance_mm,
        'levels': {'total': levels, 'passed': sum(item['passed'] for item in profile),
                   'failed': sum(not item['passed'] for item in profile)},
        'worst': [item for _, item in sorted(worst, key=lambda pair: -pair[0])[:8]],
        'profile_columns': ['y_mm', 'reference_left', 'reference_right', 'model_left', 'model_right'],
        'profile': [[item['y_mm'], *(item['reference'] or [None, None]), *(item['model'] or [None, None])]
                    for item in profile],
        'warnings': warnings,
        'method': source.method, 'cad_entities_created': 0}
    if directory is not None:
        from PIL import Image
        target = Path(directory)
        if not target.is_absolute():
            raise ProgramError('INVALID_PATH', 'directory must be absolute')
        target.mkdir(parents=True, exist_ok=True)
        base = np.asarray(entry['image'].convert('L'), dtype=np.float32)
        overlay = np.stack([base*.45 + 140]*3, axis=-1)
        overlay[np.logical_and(model, ~reference)] = (226, 74, 51)     # Model outside the reference.
        overlay[np.logical_and(reference, ~model)] = (52, 120, 196)    # Reference not covered by the model.
        path = target / f'compare_{view}_{uuid.uuid4().hex[:10]}.png'
        Image.fromarray(np.clip(overlay, 0, 255).astype(np.uint8)).save(path)
        result['images'] = [{'view': view, 'path': str(path), 'kind': 'reference_overlay',
                             'legend': 'red: model outside the reference; blue: reference not covered by the model'}]
    return result
