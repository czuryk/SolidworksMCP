"""COM-free draft extents, silhouettes and reference comparison."""
import json
import math
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from solidworks_mcp import server
from solidworks_mcp.shape_draft import (component_extents, draft_components, draft_extents, render_view,
                                        silhouettes, view_mask)
from solidworks_mcp.shape_program import run_program
from solidworks_mcp.tool_registry import dispatch_new_tool

FIXTURE = (Path(__file__).parent / 'fixtures' / 'parametric_assembly.py').read_text(encoding='utf-8')
FIGURE = ('H = model.parameter("H", 100)\n'
          'u = H / 100\n'
          'body = model.sectioned_solid("Body", [(20*u, 14*u, 10*u), (70*u, 18*u, 12*u)])\n'
          'head = model.dome("Head", radius=12*u, height=12*u, center=(0, 70*u, 0))\n'
          'leg = model.tube("LegL", [(8*u, 20*u, 0), (8*u, 0, 0)], 4*u)\n'
          'model.mirror("LegR", [leg])\n'
          'nose = model.tube("Nose", [(0, 76*u, 9*u), (0, 76*u, 22*u)], 2*u)\n'
          'model.combine("Figure", [body, head, nose])\n')


def box(code, name=None, **parameters):
    extents = draft_extents(run_program(code, parameters or None))
    return extents['bbox'] if name is None else extents['bodies'][name]


def near(actual, expected, tolerance=.02):
    return all(abs(a - e) <= tolerance for a, e in zip(actual, expected))


class ExtentTests(unittest.TestCase):
    def test_primitives_match_their_formulas(self):
        cases = {
            'model.sectioned_solid("A", [(0, 10, 8), (30, 5, 4)])': [-10, 0, -8, 10, 30, 8],
            'model.sectioned_solid("A", [(0, 10, 8), (30, 5, 4)], center_xz=(3, -2))': [-7, 0, -10, 13, 30, 6],
            'model.sectioned_solid("A", [(0, 6, 4), (20, 3, 2)], axis="X", origin=(40, 0, 10))': [40, -6, 6, 60, 6, 14],
            'model.revolve("A", [(0, 6), (16, 6)], axis="Z", origin=(8, 20, 0))': [2, 14, 0, 14, 26, 16],
            'model.revolve("A", [(5, 0), (10, 7), (20, 2)])': [-7, 5, -7, 7, 20, 7],
            'model.dome("A", radius=6, height=4, center=(10, 0, 0), axis="-X")': [6, -6, -6, 10, 6, 6],
            'model.dome("A", radius=10, height=10, center=(0, 30, 0))': [-10, 30, -10, 10, 40, 10],
            'model.ellipsoid("A", (4, 3, 2), center=(1, 2, 3))': [-3, -1, 1, 5, 5, 5],
            'model.rounded_prism("A", 28, 12, 10, 6, center=(0, 0, 5))': [-14, -6, 0, 14, 6, 10],
            'model.rounded_prism("A", 20, 8, 4, 2, center=(0, 30, 0), axis="Y")': [-10, 28, -4, 10, 32, 4],
            'model.rounded_prism("A", 10, 6, 30, 1, center=(0, -20, 5), axis="X")': [-15, -23, 0, 15, -17, 10],
            'model.tube("A", [(0, 0, 0), (0, 0, 20)], 5)': [-5, -5, 0, 5, 5, 20],
            'model.variable_tube("A", [(0, 0, 0), (0, 10, 0)], [3, 1])': [-3, 0, -3, 3, 10, 3],
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                self.assertTrue(near(box(code), expected), (box(code), expected))

    def test_oblique_tube_extent_uses_the_tilted_end_rings(self):
        result = box('model.tube("A", [(0, 0, 0), (10, 10, 0)], 2)')
        reach = 2 * math.sqrt(.5)
        self.assertTrue(near(result, [-reach, -reach, -2, 10 + reach, 10 + reach, 2], .01), result)

    def test_place_mirror_and_parameters(self):
        placed = ('a = model.rounded_prism("A", 10, 4, 2, 1)\n'
                  'model.place("P", [a], frame=model.frame(origin=(20, 0, 0), rotation=(0, 0, pi/2)))')
        self.assertTrue(near(box(placed), [18, -5, -1, 22, 5, 1]), box(placed))
        mirrored = 'a = model.dome("A", 4, 2, center=(10, 0, 0))\nmodel.mirror("M", [a])'
        extents = draft_extents(run_program(mirrored))
        self.assertTrue(near(extents['bodies']['M'], [-14, 0, -4, -6, 2, 4]))
        self.assertTrue(near(extents['bbox'], [-14, 0, -4, 14, 2, 4]))
        self.assertTrue(near(box(FIGURE, H=50), [v / 2 for v in box(FIGURE)], .02))

    def test_combines_and_estimates(self):
        union = 'a = model.dome("A", 4, 2)\nb = model.dome("B", 4, 2, center=(10, 0, 0))\nmodel.combine("C", [a, b])'
        extents = draft_extents(run_program(union))
        self.assertEqual(list(extents['bodies']), ['C'])
        self.assertEqual(extents['estimated'], [])
        self.assertTrue(near(extents['bodies']['C'], [-4, 0, -4, 14, 2, 4]))
        # A hole inside the target keeps its extents exact; a cut across its end is only an upper bound.
        inner = ('a = model.rounded_prism("A", 20, 20, 10, 2)\nb = model.tube("B", [(0, 0, -3), (0, 0, 3)], 2)\n'
                 'model.combine("C", [a, b], operation="difference")')
        self.assertEqual(draft_extents(run_program(inner))['estimated'], [])
        end = inner.replace('(0, 0, -3), (0, 0, 3)', '(0, 0, 0), (0, 0, 30)')
        self.assertEqual(draft_extents(run_program(end))['estimated'], ['C'])
        patch = ('h = model.sectioned_solid("H", [(0, 10, 10), (30, 10, 10)])\n'
                 'm = model.rounded_prism("M", 12, 6, 16, 2, center=(0, 12, 8))\n'
                 'model.surface_patch("P", h, m, 2, inward=True)')
        extents = draft_extents(run_program(patch))
        self.assertEqual(extents['estimated'], ['P'])
        self.assertEqual(sorted(extents['bodies']), ['H', 'P'])

    def test_consumed_components_are_available_on_request(self):
        program = run_program(FIGURE)
        picked = component_extents(program, ['Head', 'LegL'])
        self.assertEqual(picked['Head']['consumed_by'], 'Figure')
        self.assertTrue(near(picked['Head']['bbox'], [-12, 70, -12, 12, 82, 12]))
        self.assertNotIn('consumed_by', picked['LegL'])

    def test_reference_program_extents_and_speed(self):
        program = run_program(FIXTURE)
        started = time.perf_counter()
        extents = draft_extents(program)
        self.assertLess(time.perf_counter() - started, 2)
        self.assertEqual(len(extents['bodies']), 9)
        self.assertTrue(near(extents['bbox'][1::3], [0, 150], .01))
        # Analytic extents of the independent column and bracket fixtures.
        self.assertTrue(near(extents['bodies']['Column0'], [-12, 0, -10, 12, 150, 10], .02))
        self.assertTrue(near(extents['bodies']['BracketL'], [9, 65, 26, 21, 85, 34], .02))
        self.assertNotIn('Column0', extents['estimated'])


class SilhouetteTests(unittest.TestCase):
    def test_views_follow_the_native_standard_views(self):
        drafted = draft_components(run_program(FIGURE))
        front = silhouettes(drafted, 'front')['Figure'].bounds
        right = silhouettes(drafted, 'right')['Figure'].bounds
        left = silhouettes(drafted, 'left')['Figure'].bounds
        self.assertTrue(near(front, [-18, 20, 18, 82]))
        # Right looks from +X: the nose (+Z) points to the left of the image.
        self.assertTrue(near(right, [-22, 20, 12, 82]), right)
        self.assertTrue(near(left, [-12, 20, 22, 82]), left)

    def test_render_and_mask_agree(self):
        drafted = draft_components(run_program(FIGURE))
        with tempfile.TemporaryDirectory() as folder:
            frame = render_view(drafted, 'front', Path(folder) / 'front.png', 400)
            image = np.asarray(Image.open(frame['path']).convert('L'))
            mask = view_mask(drafted, 'front', frame, (400, 400))
            self.assertEqual(sorted(frame['bodies']), ['Figure', 'LegL', 'LegR'])
            self.assertGreater(mask.mean(), .1)
            # Every filled pixel of the image belongs to the mask (outlines are drawn on its border).
            self.assertGreater(np.logical_and(image < 250, mask).sum() / (image < 250).sum(), .98)
            height = np.flatnonzero(mask.any(axis=1))
            self.assertAlmostEqual((height[-1] - height[0] + 1) / frame['px_per_mm'], 82, delta=.6)


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def reference(self, code, view='front', size=600, name='reference.png', flip=False):
        """A synthetic reference image: the draft silhouette itself, black on white."""
        drafted = draft_components(run_program(code))
        frame = render_view(drafted, view, self.folder / 'draft.png', size)
        mask = view_mask(drafted, view, frame, (size, size))
        if flip:
            mask = mask[:, ::-1]
        path = self.folder / name
        Image.fromarray(np.where(mask, 0, 255).astype(np.uint8)).convert('RGB').save(path)
        return str(path)

    def compare(self, **arguments):
        result = dispatch_new_tool(server.sw_automation, 'compare_reference',
                                   {'directory': str(self.folder / 'out'), **arguments})
        self.assertTrue(result['success'], result)
        return result['data']

    def test_analyze_returns_predicted_extents_and_draft_images(self):
        result = dispatch_new_tool(server.sw_automation, 'execute_model_code', {
            'code': FIGURE, 'draft_views': ['front', 'right'], 'draft_components': ['Head'],
            'draft_directory': str(self.folder / 'draft')})
        self.assertTrue(result['success'], result)
        draft = result['data']['draft']
        self.assertTrue(near(draft['bbox'], [-18, 0, -12, 18, 82, 22]), draft['bbox'])
        self.assertEqual(sorted(draft['bodies']), ['Figure', 'LegL', 'LegR'])
        self.assertEqual(draft['components']['Head']['consumed_by'], 'Figure')
        self.assertEqual([Path(i['path']).name for i in result['data']['images']],
                         ['draft_front.png', 'draft_right.png'])
        self.assertTrue(all(Path(i['path']).is_file() for i in result['data']['images']))
        plain = dispatch_new_tool(server.sw_automation, 'execute_model_code', {'code': FIXTURE})
        self.assertEqual(len(plain['data']['draft']['bodies']), 9)
        self.assertNotIn('images', plain['data'])
        self.assertLessEqual(len(json.dumps(plain['data'], separators=(',', ':'))), 2700)   # Includes warnings and three numerical budget fields.

    def test_draft_options_are_validated(self):
        bad = dispatch_new_tool(server.sw_automation, 'execute_model_code', {'code': FIGURE, 'draft_views': ['iso']})
        self.assertEqual(bad['data']['error']['code'], 'INVALID_VIEWS')
        unknown = dispatch_new_tool(server.sw_automation, 'execute_model_code',
                                    {'code': FIGURE, 'draft_components': ['Missing']})
        self.assertEqual(unknown['data']['error']['code'], 'UNKNOWN_COMPONENT')
        create = server.sw_automation.execute_model_code(FIGURE, mode='create', output_path='C:/x.SLDPRT',
                                                         draft_views=['front'])
        self.assertEqual(create['data']['error']['code'], 'INVALID_ARGUMENT')

    def test_identical_program_matches_its_reference(self):
        data = self.compare(image_path=self.reference(FIGURE), code=FIGURE)
        self.assertGreater(data['iou'], .98)
        self.assertEqual(data['levels']['failed'], 0)
        self.assertEqual(data['worst'], [])
        self.assertTrue(near(data['delta_size_mm'], [0, 0], .5))
        self.assertTrue(Path(data['images'][0]['path']).is_file())
        self.assertLessEqual(len(json.dumps(data, separators=(',', ':'))), 3500)

    def test_deviation_is_located_and_signed(self):
        # The model body is 6 mm wider at its top than the reference shows.
        wider = FIGURE.replace('(70*u, 18*u, 12*u)', '(70*u, 21*u, 12*u)')
        data = self.compare(image_path=self.reference(FIGURE), code=wider, height_mm=82, align='origin')
        self.assertLess(data['iou'], .97)
        self.assertGreater(data['levels']['failed'], 0)
        worst = data['worst'][0]
        self.assertTrue(55 < worst['y_mm'] < 72, worst)
        self.assertAlmostEqual(worst['delta_right'], 3, delta=1)
        self.assertAlmostEqual(worst['delta_left'], -3, delta=1)
        self.assertAlmostEqual(worst['delta_width'], 6, delta=1.5)
        # Levels through the unchanged legs still pass.
        legs = [row for row in data['profile'] if row[0] < 15]
        self.assertTrue(legs)
        narrower = FIGURE.replace('(70*u, 18*u, 12*u)', '(70*u, 15*u, 12*u)')
        data = self.compare(image_path=self.reference(FIGURE), code=narrower, height_mm=82, align='origin')
        self.assertLess(data['worst'][0]['delta_width'], -3)

    def test_missing_part_and_height_are_reported(self):
        headless = (FIGURE.replace('head = model.dome("Head", radius=12*u, height=12*u, center=(0, 70*u, 0))\n', '')
                    .replace('[body, head, nose]', '[body, nose]'))
        data = self.compare(image_path=self.reference(FIGURE), code=headless, height_mm=82)
        self.assertLess(data['delta_size_mm'][1], -2)
        self.assertTrue(any(item.get('missing') == 'model' for item in data['worst']), data['worst'])

    def test_opposite_side_reference_is_recognised(self):
        path = self.reference(FIGURE, view='left', name='side.png')
        data = self.compare(image_path=path, code=FIGURE, view='right')
        self.assertIn('REFERENCE_MATCHES_OPPOSITE_VIEW', data['warnings'])
        self.assertEqual(data['opposite_view']['view'], 'left')
        self.assertGreater(data['opposite_view']['iou'], .98)
        self.assertNotIn('opposite_view', self.compare(image_path=path, code=FIGURE, view='left'))

    def test_saved_part_is_compared_through_its_sidecar(self):
        import hashlib
        part = self.folder / 'part.SLDPRT'
        part.write_bytes(b'native bytes')
        record = {'source': FIGURE, 'program': run_program(FIGURE, {'H': 50}),
                  'native_file_sha256': hashlib.sha256(b'native bytes').hexdigest()}
        part.with_suffix('.shape.json').write_text(json.dumps(record), encoding='utf-8')
        result = dispatch_new_tool(server.sw_automation, 'compare_reference',
                                   {'image_path': self.reference(FIGURE), 'document_path': str(part), 'height_mm': 41})
        self.assertTrue(result['success'], result)
        self.assertEqual(result['data']['parameters'], {'H': 50.0})
        self.assertGreater(result['data']['iou'], .97)
        self.assertEqual(Path(result['data']['images'][0]['path']).parent.name, 'part_views')

    def test_arguments_are_validated(self):
        path = self.reference(FIGURE)
        for arguments in ({'code': FIGURE, 'document_path': 'C:/x.SLDPRT'}, {}, {'code': FIGURE, 'view': 'iso'},
                          {'code': FIGURE, 'levels': 2}, {'code': FIGURE, 'tolerance_mm': 0},
                          {'code': FIGURE, 'align': 'left'}):
            result = dispatch_new_tool(server.sw_automation, 'compare_reference', {'image_path': path, **arguments})
            self.assertFalse(result['success'], arguments)


if __name__ == '__main__':
    unittest.main()
