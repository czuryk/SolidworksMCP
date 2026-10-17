"""Bounded, non-fatal live framing at useful shape-component boundaries."""
import itertools
import math

from .com_utils import array_r8, com_get, typed


def changed_body_names(before, after, components):
    """Follow changed evaluated nodes through consumption to live output bodies."""
    old = {node['id']: node for node in before.get('nodes', [])}
    changed = {node['id'] for node in after.get('nodes', [])
               if node != old.get(node['id'])}
    # Mirror/Boolean/attachment nodes can move although their seed names stay fixed.
    for node in after.get('nodes', []):
        if set(node.get('geometry', {}).get('seeds', [])) & changed:
            changed.add(node['id'])
    names = set()
    for name in changed:
        seen = set()
        while name in components and name not in seen:
            seen.add(name)
            component = components[name]
            names.update(component.get('bodies', []))
            name = component.get('consumed_by')
    return sorted(names)


def focus_bodies(automation, doc, names=None):
    """Frame real visible bounds, preserve orientation, and report pixel read-back.

    A view failure must never turn a valid solid into a failed CAD transaction.
    No selection, rebuild, save, body visibility or feature changes occur here.
    """
    try:
        bodies = automation._get_solid_bodies(doc)
        wanted = None if names is None else set(names)
        boxes = []
        framed = []
        for body in bodies:
            name = str(com_get(body, 'Name'))
            if wanted is not None and name not in wanted:
                continue
            if not bool(com_get(body, 'Visible')):
                continue
            box = list(com_get(body, 'GetBodyBox') or [])
            if len(box) != 6 or not all(math.isfinite(float(v)) for v in box):
                raise ValueError('Cannot read finite native body bounds: '+name)
            if any(box[i] > box[i+3] for i in range(3)):
                raise ValueError('Invalid native body bounds: '+name)
            boxes.append(box)
            framed.append(name)
        if not boxes:
            return {'status': 'NO_VISIBLE_TARGET', 'verified': False, 'bodies': []}
        lo = [min(box[i] for box in boxes) for i in range(3)]
        hi = [max(box[i+3] for box in boxes) for i in range(3)]
        result = frame_bounds(automation, doc, lo, hi)
        result['bodies'] = framed
        return result
    except Exception as exc:
        return {'status': 'WORKING_VIEW_FAILED', 'verified': False, 'message': str(exc)}


def frame_bounds(automation, doc, lo, hi):
    """Use the proven Front calibration, then restore the user's orientation.

    SW2026 ViewZoomTo2 can retain a stale translation in a rotated view.
    Calibrate viewport bias in Front, then explicitly rotate the bbox center.
    """
    view = doc.ActiveView
    if view is None:
        raise ValueError('No active model view')
    original = {'orientation': view.Orientation3, 'translation': view.Translation3,
                'scale': float(view.Scale2)}
    if bool(com_get(view, 'HasPerspective')):
        return {'status': 'WORKING_VIEW_UNSUPPORTED_PERSPECTIVE', 'verified': False}
    center = [(a+b)/2 for a, b in zip(lo, hi)]
    radius = math.dist(lo, hi)/2
    if radius <= 0:
        raise ValueError('No usable native body extent')
    points = list(itertools.product(*zip(lo, hi)))
    try:
        oriented = automation.set_view_orientation('front', zoom_to_fit=False)
        if not oriented.get('success'):
            raise ValueError('Cannot calibrate working view')
        automation._apply_zoom_bbox_m(doc, [v-radius for v in center],
                                     [v+radius for v in center], margin=.12)
        scale = float(view.Scale2)
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError('Invalid camera scale')
        utility = typed(com_get(automation.app, 'GetMathUtility'), 'IMathUtility')
        point = utility.CreatePoint(array_r8(center))
        rotated = point.MultiplyTransform(view.Orientation3).ArrayData
        bias = [t+p*scale for t, p in zip(view.Translation3.ArrayData, rotated)]
        view.Orientation3 = original['orientation']
        rotated = point.MultiplyTransform(view.Orientation3).ArrayData

        def apply_scale(value):
            view.Scale2 = value
            # Scale2 also changes Translation3; assign the correct center last.
            view.Translation3 = utility.CreateVector(array_r8(
                [b-p*value for b, p in zip(bias, rotated)]))
            doc.GraphicsRedraw2()

        apply_scale(scale)
        actual = automation._fit_measurement(doc, points)
        attempts = 1
        if not actual['verified']:
            fill = actual.get('dominant_fill_ratio', 0)
            if fill > 0 and math.isfinite(fill):
                apply_scale(scale*max(.05, min(20., .55/fill)))
                actual = automation._fit_measurement(doc, points)
                attempts += 1
        return {'status': 'WORKING_VIEW_FRAMED' if actual['verified'] else 'WORKING_VIEW_UNVERIFIED',
                'verified': bool(actual['verified']), 'measurement': actual,
                'attempts': attempts, 'orientation_preserved': True}
    except Exception:
        # Best-effort restoration is confined to camera state.
        for attr, value in [('Orientation3', original['orientation']),
                            ('Scale2', original['scale']), ('Translation3', original['translation'])]:
            try:
                setattr(view, attr, value)
            except Exception:
                pass
        raise
