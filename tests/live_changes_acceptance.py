"""Opt-in live acceptance of shape recipes and related tool behaviour.

    python tests/live_changes_acceptance.py --output <new folder> --report <json> [--only A1 A4 ...]

Every case creates its own part in the new folder through the same request
boundary the MCP server uses. Parts created here are closed at the end; no
other document is closed or saved.
"""
import argparse
import json
import math
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.automation.request_execution import execute_request
from solidworks_mcp.automation.request_journal import RequestJournal
from solidworks_mcp.automation.session_lease import SessionLease
from solidworks_mcp.tool_registry import dispatch_new_tool

HEAD = 'H = model.parameter("H", 150)\nu = H / 150\n'
A1 = HEAD + 'model.rounded_prism("Slot", 28*u, 12*u, 10*u, 6*u, center=(0, 0, 5*u))\n'
A2 = HEAD + ('slot = model.rounded_prism("Slot", 28*u, 12*u, 10*u, 6*u, center=(0, 0, 5*u))\n'
             '# Unrelated body: its topology edit must not be blocked by the fillet.\n'
             'post = model.rounded_prism("Post", 6*u, 6*u, 10*u, 1*u, center=(30*u, 0, 5*u))\n'
             '# Round only the outer loop of the front planar face.\n'
             'model.fillet("SlotRim", slot, 1.0*u, edges={"planar_loop": {"axis": "Z", "side": "max"}})\n')
A3_V1 = HEAD + ('body = model.sectioned_solid("Body", [(0, 10*u, 10*u), (30*u, 10*u, 10*u)])\n'
                'cap = model.dome("Cap", radius=10*u, height=10*u, center=(0, 30*u, 0))\n'
                'shell = model.combine("Shell", [body, cap])\n')
A3_V2 = HEAD + ('body = model.sectioned_solid("Body", [(0, 10*u, 10*u), (30*u, 10*u, 10*u)])\n'
                '# The mask reaches through the +Z side of the host only, so it clips one patch.\n'
                'mask = model.rounded_prism("Mask", 12*u, 6*u, 16*u, 2*u, center=(0, 12*u, 8*u))\n'
                'pocket = model.surface_patch("Pocket", body, mask, 2*u, inward=True)\n'
                'cap = model.dome("Cap", radius=10*u, height=10*u, center=(0, 30*u, 0))\n'
                'shell = model.combine("Shell", [body, cap])\n'
                'model.combine("Carved", [shell, pocket], operation="difference")\n')
A4 = HEAD + 'model.tube("Pin", [(0, 0, 0), (0, 0, 20*u)], 5*u)\n'
A5 = HEAD + ('# Cylinder of radius 6 and length 16 along world Z, starting at (8, 20, 0).\n'
             'model.revolve("Side", [(0, 6*u), (16*u, 6*u)], axis="Z", origin=(8*u, 20*u, 0))\n')
A6 = HEAD + ('model.sectioned_solid("Hull", [(0, 10*u, 8*u), (20*u, 10*u, 8*u), (30*u, 5*u, 4*u)], '
             'spans="linear")\n')
A6_SMOOTH = A6.replace(', spans="linear"', '')
AXES = HEAD + ('model.dome("CapX", radius=6*u, height=4*u, center=(10*u, 0, 0), axis="-X")\n'
               'model.rounded_prism("PlateY", 20*u, 8*u, 4*u, 4*u, center=(0, 30*u, 0), axis="Y")\n'
               'model.rounded_prism("BarX", 10*u, 6*u, 30*u, 1*u, center=(0, -20*u, 5*u), axis="X")\n'
               'model.sectioned_solid("ConeX", [(0, 6*u, 4*u), (20*u, 3*u, 2*u)], axis="X", '
               'origin=(40*u, 0, 10*u))\n'
               'model.tube("Rod", [(0, 50*u, 0), (10*u, 60*u, 5*u)], 2*u)\n'
               '# Oblique and driven by two parameters: the length is a native square root.\n'
               'Clr = model.parameter("Clr", 0.25)\n'
               'model.tube("Strut", [(50*u, 40*u - Clr, 0), (58*u + Clr, 52*u, 6*u)], 1.5*u + Clr)\n'
               'model.chamfer("RodBevel", "Rod", 0.5*u)\n')


class Session:
    def __init__(self, folder):
        self.folder = folder
        self.sw = SolidWorksAutomation()
        self.journal = RequestJournal(folder / 'requests.sqlite3')
        self.created = []

    def tool(self, name, **arguments):
        if name in {'execute_model_code', 'export_model'}:
            result = execute_request(self.sw, name, arguments,
                                     lambda: dispatch_new_tool(self.sw, name, arguments), self.journal)
        else:
            result = dispatch_new_tool(self.sw, name, arguments)
        assert result['success'], json.dumps(result, default=str)[:3000]
        return result['data']

    def create(self, name, code, **extra):
        path = self.folder / (name + '.SLDPRT')
        data = self.tool('execute_model_code', code=code, mode='create', output_path=str(path),
                         observe=False, **extra)
        self.created.append(path)
        return path, data

    def edit(self, path, revision, **change):
        return self.tool('execute_model_code', mode='edit', document_path=str(path),
                         expected_revision=revision, observe=False, **change)

    def inspect(self, path, revision, **extra):
        return self.tool('inspect_model', document_path=str(path), expected_revision=revision,
                         precision='delivery', **extra)


def size(body):
    return [round(b - a, 4) for a, b in zip(body['bbox']['min'], body['bbox']['max'])]


def near(actual, expected, tolerance=1e-3):
    return all(abs(a - e) <= tolerance for a, e in zip(actual, expected))


def warning_codes(answer):
    """Codes of the warnings of a mutation; the memory state of a long SolidWorks session is not a finding."""
    return [w['code'] for w in answer['warnings'] if w['code'] != 'SOLIDWORKS_MEMORY_HIGH']


def healthy(inspection):
    return all(body['kernel_health']['passed'] for body in inspection['bodies'])


def case_a1(s):
    path, made = s.create('A1_Slot', A1)
    facts = s.inspect(path, made['revision'])
    body = facts['bodies'][0]
    assert body['face_count'] == 6 and body['surface_types'] == {'plane': 4, 'cylinder': 2}, body
    assert near(size(body), [28, 12, 10]), size(body)
    assert near(body['bbox']['min'], [-14, -6, 0]) and healthy(facts)
    cycle = []
    revision = made['revision']
    for value, scale in ((120, .8), (150, 1.)):
        edited = s.edit(path, revision, parameters={'H': value})
        revision = edited['revision']
        check = s.inspect(path, revision)['bodies'][0]
        assert check['surface_types'] == {'plane': 4, 'cylinder': 2}, check
        assert near(size(check), [28*scale, 12*scale, 10*scale]), size(check)
        cycle.append({'H': value, 'size': size(check), 'timings_ms': edited['timings_ms']})
    return {'surface_types': body['surface_types'], 'size': size(body), 'cycle': cycle,
            'context_bbox_method': made.get('bbox_method')}


def case_a2(s):
    path, made = s.create('A2_Fillet', A2)
    revision = made['revision']
    facts = s.inspect(path, revision)
    rim = next(b for b in facts['bodies'] if b['name'] == 'SlotRim')
    assert rim['surface_types'].get('torus') == 2 and rim['surface_types'].get('cylinder') == 4, rim
    assert near(size(rim), [28, 12, 10]) and healthy(facts)
    steps = [{'step': 'create', 'surface_types': rim['surface_types']}]
    # 1. The slot length changes upstream: the fillet follows without a rebuild of its feature.
    edited = s.edit(path, revision, patch=[{'old': '"Slot", 28*u, 12*u', 'new': '"Slot", 34*u, 12*u'}])
    revision = edited['revision']
    rim = next(b for b in s.inspect(path, revision)['bodies'] if b['name'] == 'SlotRim')
    assert near(size(rim), [34, 12, 10]) and rim['surface_types'].get('torus') == 2, rim
    steps.append({'step': 'slot length', 'strategy': edited['edit_strategy'], 'rebuilt': edited['rebuilt_components']})
    # 2. A topology edit of the unrelated body is not blocked by the fillet.
    edited = s.edit(path, revision, patch=[{'old': '"Post", 6*u, 6*u, 10*u, 1*u', 'new': '"Post", 6*u, 6*u, 10*u, 3*u'}])
    revision = edited['revision']
    assert edited['rebuilt_components'] == ['Post'], edited
    facts = s.inspect(path, revision)
    post = next(b for b in facts['bodies'] if b['name'] == 'Post')
    assert post['surface_types'] == {'plane': 2, 'cylinder': 1}, post
    steps.append({'step': 'unrelated topology', 'rebuilt': edited['rebuilt_components']})
    # 3. The slot width changes: slot and circle recipes stay, the fillet radius follows H.
    edited = s.edit(path, revision, parameters={'H': 120})
    revision = edited['revision']
    facts = s.inspect(path, revision)
    rim = next(b for b in facts['bodies'] if b['name'] == 'SlotRim')
    assert near(size(rim), [34*.8, 12*.8, 10*.8]) and healthy(facts), size(rim)
    steps.append({'step': 'H=120', 'size': size(rim)})
    edited = s.edit(path, revision, parameters={'H': 150})
    revision = edited['revision']
    # 4. The fillet itself is edited and then moved to another edge set (topology of the fillet).
    edited = s.edit(path, revision, patch=[{'old': 'slot, 1.0*u', 'new': 'slot, 1.5*u'}])
    revision = edited['revision']
    assert edited['edit_strategy'] == 'definition_update', edited
    edited = s.edit(path, revision, patch=[{'old': '{"planar_loop": {"axis": "Z", "side": "max"}}', 'new': '"all"'}])
    revision = edited['revision']
    facts = s.inspect(path, revision)
    rim = next(b for b in facts['bodies'] if b['name'] == 'SlotRim')
    assert rim['surface_types'].get('torus') == 4 and healthy(facts), rim
    steps.append({'step': 'all edges', 'rebuilt': edited['rebuilt_components'], 'surface_types': rim['surface_types']})
    views = s.tool('observe_model', document_path=str(path), expected_revision=revision,
                   directory=str(s.folder / 'A2_views'), views=['front', 'isometric'], flat=True)
    return {'steps': steps, 'views': [image['path'] for image in views['images']]}


def case_a3(s):
    path, made = s.create('A3_Host', A3_V1)
    edited = s.edit(path, made['revision'], code=A3_V2)
    assert 'Shell' in edited['rebuilt_components'], edited
    assert edited['added_components'] == ['Mask', 'Pocket', 'Carved'], edited
    facts = s.inspect(path, edited['revision'])
    assert [b['name'] for b in facts['bodies']] == ['Carved'] and healthy(facts), facts['bodies']
    return {'rebuilt': edited['rebuilt_components'], 'added': edited['added_components'],
            'preserved_count': edited['preserved_count'], 'timings_ms': edited['timings_ms']}


def case_a4(s):
    path, made = s.create('A4_Pin', A4)
    body = s.inspect(path, made['revision'])['bodies'][0]
    assert body['face_count'] == 3 and body['surface_types'] == {'cylinder': 1, 'plane': 2}, body
    assert near(size(body), [10, 10, 20]) and near(body['bbox']['min'], [-5, -5, 0]), body['bbox']
    edited = s.edit(path, made['revision'], parameters={'H': 120})
    check = s.inspect(path, edited['revision'])['bodies'][0]
    assert near(size(check), [8, 8, 16]) and check['surface_types'] == {'cylinder': 1, 'plane': 2}, check
    return {'surface_types': body['surface_types'], 'size': size(body), 'size_H120': size(check)}


def case_a5(s):
    path, made = s.create('A5_AxisZ', A5)
    assert 'place' not in json.dumps(made['added_components']).lower()
    body = s.inspect(path, made['revision'])['bodies'][0]
    assert near(body['bbox']['min'], [2, 14, 0]) and near(body['bbox']['max'], [14, 26, 16]), body['bbox']
    revision = made['revision']
    for value, scale in ((120, .8), (150, 1.)):
        edited = s.edit(path, revision, parameters={'H': value})
        revision = edited['revision']
        check = s.inspect(path, revision)['bodies'][0]
        assert near(check['bbox']['min'], [2*scale, 14*scale, 0]) and near(check['bbox']['max'], [14*scale, 26*scale, 16*scale]), check['bbox']
    return {'bbox': body['bbox'], 'components': made['added_components']}


def radial_extent(s, path, revision, component, height):
    section = s.tool('inspect_model', document_path=str(path), expected_revision=revision,
                     sections=[{'component': component, 'center': [0, height, 0], 'normal': [0, 1, 0],
                                'samples': 64}])['sections'][0]
    return section


def case_a6(s):
    path, made = s.create('A6_Linear', A6)
    linear = radial_extent(s, path, made['revision'], 'Hull', 10)
    other, smooth_made = s.create('A6_Smooth', A6_SMOOTH)
    smooth = radial_extent(s, other, smooth_made['revision'], 'Hull', 10)
    extents = {key: [section['radius_max_mm'], section['radius_min_mm']]
               for key, section in (('linear', linear), ('smooth', smooth))}
    # Linear spans: the prismatic first span keeps the 10 x 8 semi-axes at mid height.
    assert near(extents['linear'], [10, 8], .01), extents
    # The smooth loft through the same three sections bulges there.
    assert not near(extents['smooth'], [10, 8], .01), extents
    return {'semi_axes_at_y10': extents}


def case_axes(s):
    path, made = s.create('Axes', AXES)
    facts = s.inspect(path, made['revision'])
    bodies = {b['name']: b for b in facts['bodies']}
    assert healthy(facts)
    cap = bodies['CapX']['bbox']
    assert near(cap['min'], [6, -6, -6]) and near(cap['max'], [10, 6, 6]), cap
    plate = bodies['PlateY']
    assert near(plate['bbox']['min'], [-10, 28, -4]) and near(plate['bbox']['max'], [10, 32, 4]), plate['bbox']
    assert plate['surface_types'] == {'plane': 4, 'cylinder': 2}, plate
    bar = bodies['BarX']['bbox']
    assert near(bar['min'], [-15, -23, 0]) and near(bar['max'], [15, -17, 10]), bar
    cone = bodies['ConeX']['bbox']
    assert near(cone['min'], [40, -6, 6]) and near(cone['max'], [60, 6, 14]), cone
    rod = bodies['RodBevel']
    assert rod['surface_types'] == {'cylinder': 1, 'plane': 2, 'cone': 2}, rod
    strut = bodies['Strut']
    assert strut['surface_types'] == {'cylinder': 1, 'plane': 2}, strut
    length = math.dist([50, 39.75, 0], [58.25, 52, 6])
    assert abs(strut['volume'] - math.pi * 1.75 ** 2 * length) < .01, (strut['volume'], length)
    edited = s.edit(path, made['revision'], parameters={'H': 120})
    scaled = {b['name']: b for b in s.inspect(path, edited['revision'])['bodies']}
    length = math.dist([40, 31.75, 0], [46.65, 41.6, 4.8])
    assert abs(scaled['Strut']['volume'] - math.pi * 1.45 ** 2 * length) < .01, scaled['Strut']['volume']
    assert near(scaled['CapX']['bbox']['min'], [4.8, -4.8, -4.8]), scaled['CapX']['bbox']
    assert near(scaled['ConeX']['bbox']['max'], [48, 4.8, 11.2]), scaled['ConeX']['bbox']
    return {name: body['bbox'] for name, body in bodies.items()}


def case_tools(s):
    """Path addressing, exact extents, custom views, stable STL names and replace."""
    first, made = s.create('Tools_First', A1)
    second, other = s.create('Tools_Second', A4)       # The second part is now the active document.
    facts = s.tool('inspect_model', document_path=str(first), expected_revision=made['revision'],
                   precision='delivery')
    context = s.tool('get_model_context')
    assert context['document']['path'].lower() == str(first).lower(), context['document']
    assert context['bbox_method'] == 'IBody2.GetExtremePoint', context
    assert near(context['bodies'][0]['bbox']['min'], facts['bodies'][0]['bbox']['min'], 1e-4)
    assert len(json.dumps(context)) <= 3000
    views = s.tool('observe_model', document_path=str(first), expected_revision=made['revision'],
                   directory=str(s.folder / 'Tools_views'), flat=True,
                   views=['front', {'name': 'three_quarter', 'direction': [0.574, -0.14, -0.819], 'up': [0, 1, 0]}])
    names = sorted(Path(image['path']).name for image in views['images'])
    assert names == ['front.png', 'three_quarter.png'], names
    delivery = s.folder / 'Tools_delivery'
    exported = s.tool('export_model', document_path=str(first), expected_revision=made['revision'],
                      directory=str(delivery))
    stl = sorted(f['path'] for f in exported['files'] if f['kind'] == 'stl')
    assert stl == ['stl/Slot.stl'], stl
    again = s.tool('export_model', document_path=str(first), expected_revision=made['revision'],
                   directory=str(delivery), replace=True)
    assert again['replaced_previous_delivery'] and again['status'] == 'EXPORT_VALIDATED'
    refused = dispatch_new_tool(s.sw, 'export_model', {'document_path': str(first),
                                                       'expected_revision': made['revision'],
                                                       'directory': str(delivery)})
    assert not refused['success'] and refused['data']['error']['code'] == 'OUTPUT_EXISTS', refused
    return {'active_after_inspect': context['document']['path'], 'views': names, 'stl': stl,
            'export_timings_ms': again['timings_ms'], 'context_chars': len(json.dumps(context))}


PRINT = HEAD + ('base = model.rounded_prism("Base", 30*u, 20*u, 6*u, 2*u, center=(0, 3*u, 0), axis="Y")\n'
                'post = model.tube("Post", [(0, 6*u, 0), (0, 40*u, 0)], 4*u)\n'
                'model.combine("Stand", [base, post])\n'
                '# A separate plate thinner than a printable wall, standing beside the stand.\n'
                'model.rounded_prism("Fin", 20*u, 10*u, 0.8*u, 0, center=(40*u, 5*u, 0))\n')


def case_print(s):
    """Checks on the built bodies: support, wall thickness and the native silhouette."""
    import numpy as np
    from PIL import Image
    from solidworks_mcp.shape_draft import draft_components, render_view, view_mask
    from solidworks_mcp.shape_program import run_program
    path, made = s.create('Printable', PRINT)
    facts = s.tool('inspect_model', document_path=str(path), expected_revision=made['revision'],
                   printability={'min_wall_mm': 1.2})
    report = facts['printability']
    support = report['support']
    assert support['stable'] and support['plate_mm'] == 0, support
    assert sorted(support['resting_bodies']) == ['Fin', 'Stand'], support
    walls = {wall['body']: wall for wall in report['walls']}
    assert walls['Fin']['passed'] is False and abs(walls['Fin']['min_thickness_mm'] - .8) < .01, walls
    assert walls['Stand']['passed'] is True and walls['Stand']['min_thickness_mm'] >= 5.9, walls
    assert report['walls_passed'] is False
    # The reference is the draft silhouette of the same program: the built bodies must match it.
    drafted = draft_components(run_program(PRINT))
    result = {'support': support, 'walls': walls}
    for view in ('front', 'right'):
        frame = render_view(drafted, view, s.folder / f'print_draft_{view}.png', 600)
        mask = view_mask(drafted, view, frame, (600, 600))
        image = s.folder / f'print_reference_{view}.png'
        Image.fromarray(np.where(mask, 0, 255).astype(np.uint8)).convert('RGB').save(image)
        started = time.perf_counter()
        compared = s.tool('compare_reference', image_path=str(image), view=view, document_path=str(path),
                          source='native', height_mm=40)
        assert compared['source'] == 'native' and compared['revision'] == made['revision'], compared
        assert compared['iou'] > .97 and compared['levels']['failed'] == 0, compared
        assert Path(compared['images'][0]['path']).is_file()
        result[view] = {'iou': compared['iou'], 'triangles': compared['triangles'],
                        'seconds': round(time.perf_counter() - started, 2)}
    return result


PATTERN = HEAD + ('plate = model.rounded_prism("Plate", 60*u, 12*u, 4*u, 2*u, center=(0, 0, 2*u))\n'
                  'tooth = model.prism("Tooth", [(-2*u, 0), (2*u, 0), (0, 5*u)], 4*u, center=(-24*u, 6*u, 2*u))\n'
                  'row = model.pattern("Row", [tooth], count=7, step=(8*u, 0, 0))\n'
                  'model.combine("Rack", [plate, tooth, row])\n'
                  'stud = model.tube("Stud", [(12*u, 0, 30*u), (12*u, 6*u, 30*u)], 2*u)\n'
                  'model.pattern("Studs", [stud], count=6, axis="Y", center=(0, 0, 30*u))\n'
                  'pin = model.tube("Pin", [(0, 20*u, 0), (0, 26*u, 0)], 1*u)\n'
                  'model.pattern("Pins", [pin], count=3, step=(-5*u, 0, 4*u))\n')


def case_pattern(s):
    """Native body patterns: a row united with its host, a full circle, an oblique row; edits and H."""
    def extents(facts, prefix):
        found = [b['bbox'] for b in facts['bodies'] if b['name'] == prefix or b['name'].startswith(prefix + '_')]
        assert found, (prefix, [b['name'] for b in facts['bodies']])
        return ([round(min(b['min'][i] for b in found), 3) for i in range(3)] +
                [round(max(b['max'][i] for b in found), 3) for i in range(3)]), len(found)

    def check(facts, k, pins=3, shift=-5):
        assert healthy(facts)
        rack, count = extents(facts, 'Rack')
        assert count == 1 and near(rack, [-30*k, -6*k, 0, 30*k, 11*k, 4*k], 2e-3), rack
        studs, count = extents(facts, 'Studs')
        assert count == 5 and near(studs, [-14*k, 0, 17.608*k, 8*k, 6*k, 42.392*k], 5e-3), studs
        copies, count = extents(facts, 'Pins')
        last = shift * (pins - 1)
        assert count == pins - 1, count
        # The copies run in the direction of the step: x falls while z grows.
        assert near(copies, [(last - 1)*k, 20*k, 3*k, (shift + 1)*k, 26*k, (4*(pins - 1) + 1)*k], 2e-3), copies
        return {'rack': rack, 'studs': studs, 'pins': copies}

    path, made = s.create('Pattern', PATTERN)
    result = {'create': check(s.inspect(path, made['revision']), 1), 'create_timings_ms': made.get('timings_ms')}
    scaled = s.edit(path, made['revision'], parameters={'H': 75})
    assert scaled['edit_strategy'] == 'definition_update', scaled['edit_strategy']
    result['H75'] = check(s.inspect(path, scaled['revision']), .5)
    back = s.edit(path, scaled['revision'], parameters={'H': 150})
    moved = s.edit(path, back['revision'], patch=[{'old': 'step=(-5*u, 0, 4*u)', 'new': 'step=(-6*u, 0, 4*u)'}])
    assert moved['edit_strategy'] == 'definition_update', moved['edit_strategy']
    result['step'] = check(s.inspect(path, moved['revision']), 1, shift=-6)
    more = s.edit(path, moved['revision'], patch=[{'old': 'count=3', 'new': 'count=4'}])
    assert 'Pins' in more['rebuilt_components'] and 'Rack' not in more['rebuilt_components'], more['rebuilt_components']
    result['count'] = check(s.inspect(path, more['revision']), 1, pins=4, shift=-6)
    # A new copy of a seed that a saved Boolean already consumed: the Boolean is rebuilt after it.
    added = s.edit(path, more['revision'], patch=[{
        'old': 'model.combine("Rack", [plate, tooth, row])',
        'new': ('model.pattern("Spare", [tooth], count=2, step=(0, 0, 10*u))\n'
                'model.combine("Rack", [plate, tooth, row])')}])
    assert added['added_components'] == ['Spare'] and 'Rack' in added['rebuilt_components'], added
    facts = s.inspect(path, added['revision'])
    spare, count = extents(facts, 'Spare')
    assert count == 1 and near(spare, [-26, 6, 10, -22, 11, 14], 2e-3), spare
    result['late_copy'] = {'rebuilt': added['rebuilt_components'], 'spare': spare}
    check(facts, 1, pins=4, shift=-6)
    return result


CONE = HEAD + ('Clr = model.parameter("Clr", 0.25)\n'
               '# An ellipse lofted to an exact circle: SolidWorks picked the circle connector at random.\n'
               'model.sectioned_solid("Cone", [(0, 19.4*u, 15.95*u), (7*u, 10.7*u, 10.7*u)])\n'
               'model.sectioned_solid("Stack", [(0, 15*u, 13*u), (40*u, 19*u, 16*u), (47*u - Clr, 10*u, 10*u)], '
               'spans="linear", center_xz=(60*u, 0))\n')
TIGHT = HEAD + ('model.tube("Leg", [(8.6*u, 48.2*u - 0.25, 2.2*u), (8.6*u, 45.6*u, 2.2*u), (10.2*u, 41.5*u, 2.6*u), '
                '(11.55*u, 36.8*u, 3.2*u), (12.7*u, 29.6*u, 3.7*u), (12.85*u, 23.8*u, 3.4*u)], 3.8*u)\n')
RIM = ('R = model.parameter("R", 2)\n'
       'block = model.rounded_prism("Block", 10, 10, 10, 0, center=(0, 0, 5))\n'
       'model.fillet("Rim", block, R, edges={"planar_loop": {"axis": "Z", "side": "max"}})\n')
# A vertical lead-in, a bend and a free end; a hose with one radius per point.
PATH = HEAD + ('model.tube("Leg", [(8.6*u, 48*u, 2.2*u), (8.6*u, 42.8*u, 2.2*u), (10.4*u, 37*u, 3*u), '
               '(12.2*u, 30.5*u, 3.6*u), (12.85*u, 24*u, 3.4*u)], 2.5*u)\n'
               'model.variable_tube("Hose", [(40*u, 0, 0), (40*u, 10*u, 0), (44*u, 20*u, 2*u), (52*u, 26*u, 2*u)], '
               '[3*u, 3*u, 2.5*u, 2*u])\n')


def cross_section(s, height):
    """Half extents (x, z) of every body where the plane Y = height cuts its tessellation."""
    import numpy as np
    from solidworks_mcp.automation.com_utils import command_scope
    from solidworks_mcp.automation.native_mesh import document_meshes
    with command_scope(s.sw.app):
        meshes = document_meshes(s.sw, s.sw.app.ActiveDoc)
    result = {}
    for name, triangles in meshes.items():
        found = []
        for triangle in triangles:
            for p, q in ((triangle[0], triangle[1]), (triangle[1], triangle[2]), (triangle[2], triangle[0])):
                if (p[1] - height) * (q[1] - height) < 0:
                    found.append(p + (height - p[1]) / (q[1] - p[1]) * (q - p))
        if found:
            points = np.array(found)
            result[name] = [round(float(np.ptp(points[:, 0]) / 2), 3), round(float(np.ptp(points[:, 2]) / 2), 3)]
    return result


def case_cone(s):
    """Lofts between an ellipse and an exact circle are ruled, not twisted, and kernel-valid."""
    path, made = s.create('Cone', CONE)
    assert warning_codes(made) == [], made['warnings']
    facts = s.inspect(path, made['revision'])
    assert healthy(facts), [b['kernel_health'] for b in facts['bodies']]
    low = cross_section(s, 3.5)                       # Half way up the plain cone.
    assert near(low['Cone'], [15.05, 13.325], 2e-2), low
    high = cross_section(s, 43.375)                   # Half way up the top span of the stack.
    assert near(high['Stack'], [14.5, 13.0], 2e-2), high
    tops = {b['name']: b['bbox']['max'][1] for b in facts['bodies']}
    assert abs(tops['Cone'] - 7) < 1e-6 and abs(tops['Stack'] - 46.75) < 1e-6, tops
    return {'cone_mid': low['Cone'], 'stack_mid': high['Stack'], 'tops': tops}


def case_diag(s):
    """A failed rebuild names its feature; a tube that bends too tightly is announced before it fails."""
    from solidworks_mcp.error_actions import attach_recovery
    analysed = s.tool('execute_model_code', code=TIGHT)
    warning = analysed['warnings'][0]
    assert warning['code'] == 'TUBE_BEND_TOO_TIGHT' and warning['component'] == 'Leg', analysed['warnings']
    # The tube and the fillet share one part: a failure must name its own cause, not the other warning.
    rim, built = s.create('Diag_Rim', TIGHT + RIM)
    assert warning_codes(built) == ['TUBE_BEND_TOO_TIGHT'], built['warnings']

    def failing_edit(**parameters):
        arguments = {'mode': 'edit', 'document_path': str(rim), 'expected_revision': built['revision'],
                     'parameters': parameters, 'observe': False}
        answer = execute_request(s.sw, 'execute_model_code', arguments,
                                 lambda: dispatch_new_tool(s.sw, 'execute_model_code', arguments), s.journal)
        return attach_recovery('execute_model_code', arguments, answer)

    failed = failing_edit(R=6)
    assert not failed['success'], failed
    error = failed['data']['error']
    assert error['code'] == 'NATIVE_REBUILD_FAILED', error
    assert error['details']['features'][0]['feature'] == 'Rim', error['details']
    assert error['details']['features'][0]['type'] == 'Fillet', error['details']
    assert error['details']['component'] == 'Rim' and 'Rim (Fillet)' in error['message'], error
    assert error['document_restored'] is True, error
    assert error['recommended_actions'][0]['arguments'] == {'components': ['Rim']}, error['recommended_actions']
    assert error['likely_causes'] == [] and error['stage'] == 'native_edit', error
    assert [w['component'] for w in error['details']['program_warnings']] == ['Leg'], error['details']
    context = s.tool('get_model_context')
    assert context['revision'] == built['revision'], 'the failed edit must leave the saved state'
    # Scaling the model down makes the tight sweep fail in SolidWorks 2026: then the tube is the cause.
    scaled = failing_edit(H=120)
    sweep = None
    if not scaled['success']:
        sweep = scaled['data']['error']
        assert sweep['details']['component'] == 'Leg' and 'Leg (Sweep)' in sweep['message'], sweep
        assert len(sweep['likely_causes']) == 1 and sweep['likely_causes'][0].startswith('Leg: TUBE_BEND_TOO_TIGHT'), sweep
        assert s.tool('get_model_context')['revision'] == built['revision']
    return {'bend': {k: warning[k] for k in ('min_bend_radius_mm', 'tube_radius_mm', 'between_points')},
            'failure': error['message'], 'actions': [a['tool'] for a in error['recommended_actions']],
            'scaled_sweep': sweep['message'] if sweep else 'built'}


def case_path(s):
    """Interpolated paths are solved in the direction they are planned in: no reversal, no extra rebuild."""
    from solidworks_mcp.automation.shape_parametric import read_record
    path, made = s.create('Path', PATH)
    assert warning_codes(made) == [], made['warnings']
    record = read_record(s.sw.app.ActiveDoc)
    curves = [c for c in record['native']['curves'] if '_Path' in c['sketch']]
    assert len(curves) == 7, [c['sketch'] for c in curves]
    assert not any(c.get('parameter_reversed') for c in curves), [c['sketch'] for c in curves if c.get('parameter_reversed')]
    recipes = {name: record['native']['components'][name].get('recipe') for name in ('Leg', 'Hose')}
    assert recipes == {'Leg': 'path_chord_v2', 'Hose': 'path_chord_v2'}, recipes
    facts = s.inspect(path, made['revision'])
    assert healthy(facts)
    sizes = {b['name']: size(b) for b in facts['bodies']}
    revision, cycle = made['revision'], []
    for value, scale in ((120, .8), (150, 1.)):
        edited = s.edit(path, revision, parameters={'H': value})
        revision = edited['revision']
        check = s.inspect(path, revision)
        assert healthy(check)
        for body in check['bodies']:
            assert near(size(body), [v*scale for v in sizes[body['name']]], 2e-3), (body['name'], size(body))
        cycle.append({'H': value, 'timings_ms': edited['timings_ms']})
    # Moving the free end rewrites the native curves in place.
    moved = s.edit(path, revision, patch=[{'old': '(12.85*u, 24*u, 3.4*u)', 'new': '(13.5*u, 22*u, 3.4*u)'}])
    assert moved['edit_strategy'] == 'definition_update' and moved['updated_curve_count'] >= 1, moved
    after = s.inspect(path, moved['revision'])
    assert healthy(after)
    leg = next(b for b in after['bodies'] if b['name'] == 'Leg')
    # The end face is square to the last chord (1.3, -8.5, -0.2), so its rim dips below the end point.
    lowest = 22 - 2.5 * math.hypot(1.3, .2) / math.hypot(1.3, 8.5, .2)
    assert abs(leg['bbox']['min'][1] - lowest) < 0.02, (leg['bbox'], lowest)
    record = read_record(s.sw.app.ActiveDoc)
    assert not any(c.get('parameter_reversed') for c in record['native']['curves'] if '_Path' in c['sketch'])
    return {'path_curves': len(curves), 'create_timings_ms': made['timings_ms'], 'cycle': cycle,
            'moved_timings_ms': moved['timings_ms']}


def case_templates(s):
    """Every library template builds as written, is healthy, and keeps its clearance."""
    from solidworks_mcp.shape_templates import TEMPLATES
    result = {}
    for name, item in TEMPLATES.items():
        path, made = s.create('Template_' + name, item['code'])
        clearance = (made.get('parameters') or {}).get('Clr')
        pairs = [{'id': a + '-' + b, 'a': {'body': a}, 'b': {'body': b},
                  'min_clearance_mm': .2, 'max_clearance_mm': .35} for a, b in item['fits']]
        facts = s.inspect(path, made['revision'], printability=True, **({'body_pairs': pairs} if pairs else {}))
        assert sorted(body['name'] for body in facts['bodies']) == sorted(item['bodies']), (name, facts['bodies'])
        assert healthy(facts), name
        gaps = {pair['id']: pair for pair in facts.get('body_pairs', [])}
        for pair in gaps.values():
            assert pair['clearance_check']['passed'] is True, (name, pair)
        walls = {wall['body']: wall.get('min_thickness_mm') for wall in facts['printability']['walls']}
        result[name] = {'gaps': {key: round(pair['clearance_mm'], 4)
                                 for key, pair in gaps.items()},
                        'walls': walls, 'stable': facts['printability']['support']['stable'],
                        'clearance': clearance}
    return result


def case_surface(s):
    """The documented recess-and-insert example runs as written and keeps its clearance."""
    from solidworks_mcp.automation.shape_service import API
    path, made = s.create('SurfaceExample', API['surface_example'])
    facts = s.inspect(path, made['revision'], body_pairs=[
        {'id': 'fit', 'a': {'body': 'Carved'}, 'b': {'body': 'Insert'},
         'min_clearance_mm': .20, 'max_clearance_mm': .30}])
    pair = facts['body_pairs'][0]
    assert pair['clearance_check']['passed'] is True and pair['relation'] == 'SEPARATED', pair
    assert healthy(facts)
    edited = s.edit(path, made['revision'], parameters={'H': 80})
    pair = s.inspect(path, edited['revision'], body_pairs=[
        {'id': 'fit', 'a': {'body': 'Carved'}, 'b': {'body': 'Insert'},
         'min_clearance_mm': .20, 'max_clearance_mm': .30}])['body_pairs'][0]
    assert pair['clearance_check']['passed'] is True, pair
    return {'clearance_mm': facts['body_pairs'][0]['clearance_mm'], 'clearance_mm_H80': pair['clearance_mm']}


G4 = HEAD + ('model.rounded_prism("Box", 20*u, 10*u, 6*u, 0, center=(0, 0, 3*u))\n'
             'model.prism("Bracket", [(0, 0), (30*u, 0), (30*u, 6*u), (6*u, 6*u), (6*u, 24*u), (0, 24*u)], 10*u, '
             'center=(40*u, 0, 5*u))\n'
             'model.prism("Fin", [(0, 0), (10*u, 0), (0, 4*u)], 6*u, center=(0, 30*u, 0), axis="Y")\n'
             'model.revolve("Ring", [(0, 8*u), (4*u, 8*u), (4*u, 10*u), (0, 10*u)], closed=True, '
             'origin=(0, -20*u, 0))\n')


def case_g4(s):
    """Polygon prism, plain box and closed revolve."""
    path, made = s.create('G4_Primitives', G4)
    draft = dispatch_new_tool(s.sw, 'execute_model_code', {'code': G4})['data']['draft']
    facts = s.inspect(path, made['revision'])
    assert healthy(facts)
    bodies = {b['name']: b for b in facts['bodies']}
    assert bodies['Box']['surface_types'] == {'plane': 6}, bodies['Box']
    assert near(bodies['Box']['bbox']['min'] + bodies['Box']['bbox']['max'], [-10, -5, 0, 10, 5, 6])
    assert bodies['Bracket']['surface_types'] == {'plane': 8}, bodies['Bracket']
    assert near(bodies['Bracket']['bbox']['min'] + bodies['Bracket']['bbox']['max'], [40, 0, 0, 70, 24, 10])
    assert abs(bodies['Bracket']['volume'] - 2880) < .01, bodies['Bracket']['volume']
    assert near(bodies['Fin']['bbox']['min'] + bodies['Fin']['bbox']['max'], [0, 27, 0, 10, 33, 4]), bodies['Fin']['bbox']
    assert abs(bodies['Fin']['volume'] - 120) < .01, bodies['Fin']['volume']
    assert near(bodies['Ring']['bbox']['min'] + bodies['Ring']['bbox']['max'], [-10, -20, -10, 10, -16, 10])
    assert abs(bodies['Ring']['volume'] - math.pi * (100 - 64) * 4) < .01, bodies['Ring']['volume']
    # The COM-free draft predicted the same extents before anything was built.
    for name, body in bodies.items():
        assert near(draft['bodies'][name], body['bbox']['min'] + body['bbox']['max'], .05), (name, draft['bodies'][name])
    edited = s.edit(path, made['revision'], parameters={'H': 120})
    scaled = {b['name']: b for b in s.inspect(path, edited['revision'])['bodies']}
    assert abs(scaled['Bracket']['volume'] - 2880 * .512) < .01 and scaled['Box']['surface_types'] == {'plane': 6}
    return {name: body['surface_types'] for name, body in bodies.items()}


def case_revert(s):
    """mode=revert restores earlier saved states without a rebuild."""
    path, made = s.create('Revert', A1)
    first = s.edit(path, made['revision'], parameters={'H': 120})
    second = s.edit(path, first['revision'], patch=[{'old': '"Slot", 28*u', 'new': '"Slot", 40*u'}])
    assert near(size(s.inspect(path, second['revision'])['bodies'][0]), [32, 9.6, 8])
    started = time.perf_counter()
    back = s.tool('execute_model_code', mode='revert', document_path=str(path),
                  expected_revision=second['revision'], observe=False)
    seconds = round(time.perf_counter() - started, 1)
    assert back['edit_strategy'] == 'restore_saved_state' and back['history_depth'] == 1, back
    assert back['parameters'] == {'H': 120.0}, back['parameters']
    assert near(size(s.inspect(path, back['revision'])['bodies'][0]), [22.4, 9.6, 8])
    source = s.tool('get_model_context', include_source=True)['source']
    assert '"Slot", 28*u' in source
    again = s.tool('execute_model_code', mode='revert', document_path=str(path),
                   expected_revision=back['revision'], observe=False)
    assert again['parameters'] == {'H': 150.0} and again['history_depth'] == 0
    assert near(size(s.inspect(path, again['revision'])['bodies'][0]), [28, 12, 10])
    empty = execute_request(s.sw, 'execute_model_code', {'mode': 'revert', 'document_path': str(path),
                                                         'expected_revision': again['revision']},
                            lambda: dispatch_new_tool(s.sw, 'execute_model_code', {
                                'mode': 'revert', 'document_path': str(path),
                                'expected_revision': again['revision']}), s.journal)
    assert not empty['success'] and empty['data']['error']['code'] == 'NO_HISTORY', empty
    # The first edit again, from the state it started from: a new attempt, not a refused replay.
    assert again['revision'] == made['revision'], (again['revision'], made['revision'])
    repeated = s.edit(path, again['revision'], parameters={'H': 120})
    assert repeated['request_receipt']['attempt'] == 2 and not repeated['request_receipt']['replayed'], repeated
    assert near(size(s.inspect(path, repeated['revision'])['bodies'][0]), [22.4, 9.6, 8])
    # The part is still editable after the reverts.
    after = s.edit(path, repeated['revision'], parameters={'H': 135})
    assert near(size(s.inspect(path, after['revision'])['bodies'][0]), [25.2, 10.8, 9])
    return {'revert_seconds': seconds, 'revert_timings_ms': back['timings_ms']}


CASES = {'SURFACE': case_surface, 'G4': case_g4, 'REVERT': case_revert, 'A1': case_a1, 'A2': case_a2, 'A3': case_a3, 'A4': case_a4, 'A5': case_a5, 'A6': case_a6,
         'AXES': case_axes, 'TOOLS': case_tools, 'PRINT': case_print, 'TEMPLATES': case_templates, 'PATTERN': case_pattern, 'CONE': case_cone,
         'DIAG': case_diag, 'PATH': case_path}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', required=True, help='New folder; must not exist')
    parser.add_argument('--report', required=True)
    parser.add_argument('--only', nargs='+', choices=sorted(CASES), default=list(CASES))
    parser.add_argument('--keep-open', action='store_true')
    options = parser.parse_args()
    folder = Path(options.output).resolve()
    if folder.exists():
        raise SystemExit('Output folder already exists: ' + str(folder))
    folder.mkdir(parents=True)
    session = Session(folder)
    report = {}
    with SessionLease():
        assert session.sw.connect()['success']
        for name in options.only:
            started = time.perf_counter()
            try:
                report[name] = {'passed': True, 'result': CASES[name](session)}
            except Exception as exc:
                report[name] = {'passed': False, 'error': str(exc)[:4000], 'traceback': traceback.format_exc()[-3000:]}
            report[name]['seconds'] = round(time.perf_counter() - started, 1)
            print(name, 'PASS' if report[name]['passed'] else 'FAIL', report[name]['seconds'],
                  '' if report[name]['passed'] else report[name]['error'][:1500], flush=True)
        if not options.keep_open:
            for path in session.created:
                doc = session.sw.app.GetOpenDocumentByName(str(path))
                if doc is not None:
                    session.sw.app.CloseDoc(session.sw._get_doc_title(doc))
    Path(options.report).write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
    print('ACCEPTANCE', json.dumps({name: item['passed'] for name, item in report.items()}), flush=True)
    return 0 if all(item['passed'] for item in report.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
