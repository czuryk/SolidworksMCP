"""Bounded raster measurements, independent of CAD and tracing backends."""

from __future__ import annotations

import hashlib
import io
import json
import math
from collections import OrderedDict
from pathlib import Path
from statistics import median

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .shape_program import ProgramError

_CACHE = OrderedDict()
_CACHE_LIMIT = 4
_MAX_PIXELS = 12_000_000


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_keys(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ProgramError('INVALID_REFERENCE_QUERY', f'Expected keys {sorted(allowed)}; required {sorted(required)}')


# Which expected dimensions each measurement kind can verify.
EXPECTED_KEYS = {'bbox': {'width', 'height', 'center_x', 'center_y'},
                 'center': {'center_x', 'center_y'},
                 'width_at_height': {'width'}, 'depth_range': {'width'}}


def _check(query, result, scale, origin):
    """Tri-state comparison with the model size: true, false, or null when unknown."""
    expected = query['expected_mm']
    tolerance = query.get('tolerance_mm')
    check = {'expected_mm': expected, 'tolerance_mm': tolerance, 'delta_mm': None, 'passed': None}
    if result.get('status') != 'ESTIMATE':
        return {**check, 'reason': result.get('reason', 'NO_MEASUREMENT')}
    if scale is None:
        return {**check, 'reason': 'CALIBRATION_REQUIRED'}
    left, top, right, bottom = result['bbox_px']
    center_x, center_y = (left + right) / 2, (top + bottom) / 2
    if origin is not None:
        # Model frame: origin at the given pixel, Y up.
        center_x, center_y = center_x - origin[0], origin[1] - center_y
    measured = {'width': (right - left) * scale, 'height': (bottom - top) * scale,
                'center_x': center_x * scale, 'center_y': center_y * scale}
    check['measured_mm'] = {key: round(measured[key], 3) for key in expected}
    check['delta_mm'] = {key: round(measured[key] - expected[key], 3) for key in expected}
    check['center_frame'] = 'model_origin_y_up' if origin is not None else 'image_top_left_y_down'
    if 'FOREGROUND_TOUCHES_ROI_BOUNDARY' in result.get('warnings', []):
        # A clipped extent is not the element's extent.
        return {**check, 'reason': 'FOREGROUND_TOUCHES_ROI_BOUNDARY'}
    if tolerance is None:
        return {**check, 'reason': 'TOLERANCE_NOT_GIVEN'}
    check['passed'] = all(abs(delta) <= tolerance for delta in check['delta_mm'].values())
    return check


def _load(path, background, threshold):
    path = Path(path)
    if not path.is_absolute() or not path.is_file() or path.stat().st_size > 32_000_000:
        raise ProgramError('INVALID_REFERENCE_PATH', 'Use an absolute local image path, at most 32 MB')
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    key = (digest, tuple(background) if background is not None else None, threshold)
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key], True
    with Image.open(io.BytesIO(raw)) as opened:
        if opened.width * opened.height > _MAX_PIXELS:
            raise ProgramError('REFERENCE_TOO_LARGE', 'Reference exceeds 12 million pixels')
        rgba = opened.convert('RGBA')
        image = Image.new('RGBA', rgba.size, (255, 255, 255, 255))
        image.alpha_composite(rgba)
        image = image.convert('RGB')
    corners = [image.getpixel(p) for p in [(0, 0), (image.width-1, 0),
                                          (0, image.height-1), (image.width-1, image.height-1)]]
    bg = background if background is not None else [int(median(c[i] for c in corners)) for i in range(3)]
    spread = max(max(abs(c[i]-bg[i]) for i in range(3)) for c in corners)
    bands = ImageChops.difference(image, Image.new('RGB', image.size, tuple(bg))).split()
    distance = ImageChops.lighter(ImageChops.lighter(bands[0], bands[1]), bands[2])
    masks = [distance.point(lambda x, t=t: 255 if x >= t else 0)
             for t in (max(1, threshold-8), threshold, min(254, threshold+8))]
    entry = {'sha256': digest, 'image': image, 'masks': masks, 'background': bg,
             'background_uncertain': background is None and spread > 16}
    _CACHE[key] = entry
    while len(_CACHE) > _CACHE_LIMIT:
        _CACHE.popitem(last=False)
    return entry, False


def _colour_masks(image, colour, tolerance, cache):
    """Masks of the pixels close to one colour, at the tolerance and 8 levels around it."""
    key = (tuple(colour), tolerance)
    if key not in cache:
        bands = ImageChops.difference(image, Image.new('RGB', image.size, tuple(colour))).split()
        distance = ImageChops.lighter(ImageChops.lighter(bands[0], bands[1]), bands[2])
        # Opened by 3 px: anti-aliased outlines pass through any colour and would stretch the box.
        cache[key] = [distance.point(lambda x, t=t: 255 if x <= t else 0)
                      .filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
                      for t in (max(1, tolerance - 8), tolerance, min(254, tolerance + 8))]
    return cache[key]


def measure_reference(image_path, queries, projection='unknown', calibration=None,
                      background=None, threshold=32, annotation_directory=None):
    """Measure visible raster extents in explicit ROIs, never semantic hidden geometry."""
    if projection not in {'front', 'right', 'top', 'perspective', 'unknown'}:
        raise ProgramError('INVALID_PROJECTION', 'Use front, right, top, perspective or unknown')
    if not isinstance(threshold, int) or isinstance(threshold, bool) or not 9 <= threshold <= 246:
        raise ProgramError('INVALID_THRESHOLD', 'threshold must be an integer from 9 to 246')
    if background is not None and (not isinstance(background, list) or len(background) != 3 or
            any(not isinstance(x, int) or isinstance(x, bool) or not 0 <= x <= 255 for x in background)):
        raise ProgramError('INVALID_BACKGROUND', 'background must contain three RGB bytes')
    if not isinstance(queries, list) or not 1 <= len(queries) <= 12:
        raise ProgramError('INVALID_REFERENCE_QUERY', 'Supply one to twelve targeted queries')
    scale = None
    origin = None
    if calibration is not None:
        _check_keys(calibration, {'pixel_distance', 'length_mm', 'origin_px'}, {'pixel_distance', 'length_mm'})
        if any(not _number(calibration[k]) or calibration[k] <= 0 for k in ('pixel_distance', 'length_mm')):
            raise ProgramError('INVALID_CALIBRATION', 'Calibration distances must be finite and positive')
        origin = calibration.get('origin_px')
        if origin is not None and (not isinstance(origin, list) or len(origin) != 2 or
                                   any(not _number(v) for v in origin)):
            raise ProgramError('INVALID_CALIBRATION', 'origin_px is the [x, y] pixel of the model origin')
        if projection in {'unknown', 'perspective'}:
            raise ProgramError('UNCALIBRATED_PROJECTION', 'Metric conversion requires a declared orthographic view')
        scale = calibration['length_mm'] / calibration['pixel_distance']
    output = None
    if annotation_directory is not None:
        output = Path(annotation_directory)
        if not output.is_absolute():
            raise ProgramError('INVALID_PATH', 'Annotation directory must be absolute')
    entry, hit = _load(image_path, background, threshold)
    image = entry['image']
    results, images, ids = [], [], set()
    # Validate the whole request before writing any annotation artifacts.
    for q in queries:
        _check_keys(q, {'id', 'kind', 'region', 'y', 'visibility', 'expected_mm', 'tolerance_mm',
                        'color', 'color_tolerance'}, {'id', 'kind', 'region'})
        if 'color' in q and (not isinstance(q['color'], list) or len(q['color']) != 3 or
                             any(not isinstance(x, int) or isinstance(x, bool) or not 0 <= x <= 255 for x in q['color'])):
            raise ProgramError('INVALID_REFERENCE_QUERY', 'color must contain three RGB bytes')
        if 'color_tolerance' in q and ('color' not in q or not isinstance(q['color_tolerance'], int) or
                                       isinstance(q['color_tolerance'], bool) or not 4 <= q['color_tolerance'] <= 120):
            raise ProgramError('INVALID_REFERENCE_QUERY', 'color_tolerance is an integer from 4 to 120 and needs color')
        if not isinstance(q['id'], str) or not q['id'] or len(q['id']) > 80 or q['id'] in ids:
            raise ProgramError('INVALID_REFERENCE_QUERY', 'Query IDs must be unique nonempty strings, at most 80 characters')
        ids.add(q['id'])
        if q['kind'] not in {'bbox', 'center', 'aspect_ratio', 'width_at_height', 'depth_range'}:
            raise ProgramError('INVALID_REFERENCE_QUERY', 'Unsupported measurement kind')
        roi = q['region']
        if (not isinstance(roi, list) or len(roi) != 4 or
                any(not isinstance(x, int) or isinstance(x, bool) for x in roi) or
                not 0 <= roi[0] < roi[2] <= image.width or not 0 <= roi[1] < roi[3] <= image.height):
            raise ProgramError('INVALID_REFERENCE_QUERY', 'region is [left, top, right, bottom] in image pixels, exclusive right/bottom')
        if q.get('visibility', 'visible') not in {'visible', 'occluded', 'unknown'}:
            raise ProgramError('INVALID_REFERENCE_QUERY', 'visibility must be visible, occluded or unknown')
        if q['kind'] in {'width_at_height', 'depth_range'}:
            if not isinstance(q.get('y'), int) or isinstance(q.get('y'), bool) or not roi[1] <= q['y'] < roi[3]:
                raise ProgramError('INVALID_REFERENCE_QUERY', 'Row measurements require integer image-space y within region')
        elif 'y' in q:
            raise ProgramError('INVALID_REFERENCE_QUERY', 'y is only supported for row measurements')
        if 'tolerance_mm' in q and (not _number(q['tolerance_mm']) or q['tolerance_mm'] <= 0):
            raise ProgramError('INVALID_REFERENCE_QUERY', 'tolerance_mm must be finite and positive')
        if 'expected_mm' in q:
            allowed = EXPECTED_KEYS.get(q['kind'], set())
            expected = q['expected_mm']
            if (not isinstance(expected, dict) or not expected or set(expected) - allowed or
                    any(not _number(v) for v in expected.values())):
                raise ProgramError('INVALID_REFERENCE_QUERY',
                    f"expected_mm for {q['kind']} accepts numbers for {sorted(allowed) or 'no keys'}")
        elif 'tolerance_mm' in q:
            raise ProgramError('INVALID_REFERENCE_QUERY', 'tolerance_mm needs expected_mm')
    coloured = {}
    for index, q in enumerate(queries):
        roi = q['region']; kind = q['kind']; warnings = []
        # A colour query measures the pixels of that colour instead of everything off the background.
        masks = (_colour_masks(image, q['color'], q.get('color_tolerance', 30), coloured) if 'color' in q
                 else entry['masks'])
        result = {'id': q['id'], 'kind': kind, 'region_px': roi,
                  'visibility': q.get('visibility', 'visible'), 'confidence': 'unknown'}
        reason = None
        if result['visibility'] != 'visible':
            reason = 'OCCLUDED_OR_UNKNOWN_BOUNDARY'
        if kind == 'depth_range' and projection != 'right':
            reason = 'DEPTH_REQUIRES_RIGHT_ORTHOGRAPHIC_VIEW'
        if reason:
            result.update(status='UNKNOWN', reason=reason, value=None)
            if 'expected_mm' in q:
                result['check'] = _check(q, result, scale, origin)
            results.append(result)
            continue
        row = kind in {'width_at_height', 'depth_range'}
        box = [roi[0], q['y'], roi[2], q['y']+1] if row else roi
        bounds = [mask.crop(box).getbbox() for mask in masks]
        if any(b is None for b in bounds):
            result.update(status='UNKNOWN', reason='NO_STABLE_FOREGROUND', value=None)
            if 'expected_mm' in q:
                result['check'] = _check(q, result, scale, origin)
            results.append(result)
            continue
        bounds = [[b[0]+box[0], b[1]+box[1], b[2]+box[0], b[3]+box[1]] for b in bounds]
        b = bounds[1]; w, h = b[2]-b[0], b[3]-b[1]
        clipped = b[0] == roi[0] or b[2] == roi[2] or (not row and (b[1] == roi[1] or b[3] == roi[3]))
        spread = max(max(v[i] for v in bounds)-min(v[i] for v in bounds) for i in range(4))
        if clipped: warnings.append('FOREGROUND_TOUCHES_ROI_BOUNDARY')
        if entry['background_uncertain']: warnings.append('NONUNIFORM_OR_UNKNOWN_BACKGROUND')
        if spread > 4: warnings.append('THRESHOLD_SENSITIVE_BOUNDARY')
        if row:
            pixels = masks[1].crop((b[0], q['y'], b[2], q['y']+1)).tobytes()
            # An outline may contain a white interior; report the exterior span,
            # never pretend that all of it is a filled or single semantic region.
            if pixels.count(0) > len(pixels)*.1: warnings.append('GAPS_IN_SPAN_OR_OUTLINE_ONLY')
            value = w
            interval = [max(0, min(v[2]-v[0] for v in bounds)-2), max(v[2]-v[0] for v in bounds)+2]
        elif kind == 'center':
            value = [(b[0]+b[2])/2, (b[1]+b[3])/2]
            interval = [[min((v[i]+v[i+2])/2 for v in bounds)-1,
                         max((v[i]+v[i+2])/2 for v in bounds)+1] for i in range(2)]
        elif kind == 'aspect_ratio':
            value = w/h
            interval = [max(0, min((v[2]-v[0]-2)/(v[3]-v[1]+2) for v in bounds)),
                        max((v[2]-v[0]+2)/max(1, v[3]-v[1]-2) for v in bounds)]
        else:
            value = b
            interval = [[min(v[i] for v in bounds)-1, max(v[i] for v in bounds)+1] for i in range(4)]
        result.update(status='ESTIMATE', value=value, interval=interval,
                      unit='ratio' if kind == 'aspect_ratio' else 'px', bbox_px=b,
                      confidence='low' if warnings else 'medium', warnings=warnings)
        if scale is not None and kind != 'aspect_ratio':
            def convert(v):
                return [convert(x) for x in v] if isinstance(v, list) else round(v*scale, 3)
            result['metric'] = {'value': convert(value), 'interval': convert(interval), 'unit': 'mm',
                                'origin': 'image_top_left', 'y_direction': 'down'}
        if 'expected_mm' in q:
            result['check'] = _check(q, result, scale, origin)
        results.append(result)
        if output is not None:
            output.mkdir(parents=True, exist_ok=True)
            crop = image.crop(roi); draw = ImageDraw.Draw(crop)
            local = (b[0]-roi[0], b[1]-roi[1], b[2]-roi[0]-1, b[3]-roi[1]-1)
            if row: draw.line(local, fill='#ed3030', width=3)
            else: draw.rectangle(local, outline='#ed3030', width=2)
            crop.thumbnail((650, 650))
            tag = hashlib.sha256(json.dumps([entry['sha256'], q, threshold, background], sort_keys=True).encode()).hexdigest()[:20]
            path = output / f'reference_{tag}_{index}.png'
            crop.save(path)
            images.append({'query_id': q['id'], 'path': str(path), 'reference_sha256': entry['sha256']})
    checks = [r['check']['passed'] for r in results if 'check' in r]
    summary = ({'checks': {'total': len(checks), 'passed': checks.count(True),
                           'failed': checks.count(False), 'unknown': checks.count(None)}} if checks else {})
    return {'reference': {'path': str(Path(image_path)), 'sha256': entry['sha256'],
                          'size_px': list(image.size), 'projection': projection},
            'measurements': results, 'images': images, 'cache_hit': hit, **summary,
            'method': 'visible_foreground_extent_with_threshold_sensitivity',
            'uncertainty': 'Heuristic raster interval, not statistical confidence or manufacturing tolerance. ROI and visibility are caller supplied; semantic separation and hidden depth are not inferred.',
            'cad_entities_created': 0}
