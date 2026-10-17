"""Offline checks of the fixes that came out of the end-to-end retest through the MCP client."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

from solidworks_mcp import server
from solidworks_mcp.automation.shape_parametric import (ParametricCompiler, feature_owners, rebuild_error,
                                                        rebuild_failures)
from solidworks_mcp.error_actions import recovery_actions
from solidworks_mcp.mesh_checks import MeshSilhouette, wall_thickness
from solidworks_mcp.reference_compare import compare_reference
from solidworks_mcp.reference_measure import measure_reference
from solidworks_mcp.shape_checks import bend_radii, program_warnings
from solidworks_mcp.shape_draft import draft_components, draft_extents
from solidworks_mcp.shape_program import ProgramError, run_program
from solidworks_mcp.shape_responses import compact_analysis, compact_export
from solidworks_mcp.tool_registry import dispatch_new_tool

# The leg of the first retest build: a short vertical lead-in, then a sharp turn.
TIGHT = ('H = model.parameter("H", 150)\nu = H / 150\n'
         'model.tube("Leg", [(8.6*u, 48.2*u - 0.25, 2.2*u), (8.6*u, 45.6*u, 2.2*u), (10.2*u, 41.5*u, 2.6*u), '
         '(11.55*u, 36.8*u, 3.2*u), (12.7*u, 29.6*u, 3.7*u)], 3.8*u)\n')
GENTLE = ('H = model.parameter("H", 150)\nu = H / 150\n'
          'model.tube("Leg", [(8.6*u, 48.2*u - 0.25, 2.2*u), (8.6*u, 42.8*u, 2.2*u), (10.4*u, 37.0*u, 3.0*u), '
          '(12.2*u, 30.5*u, 3.6*u), (12.85*u, 24.0*u, 3.4*u)], 3.8*u)\n')


def box(low, high):
    (x0, y0, z0), (x1, y1, z1) = low, high
    p = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    quads = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (2, 3, 7, 6), (1, 2, 6, 5), (0, 4, 7, 3)]
    return np.array([[p[a], p[b], p[c]] for q in quads for a, b, c in ((q[0], q[1], q[2]), (q[0], q[2], q[3]))], float)


def refined(triangles, levels):
    """Each triangle split into four, repeatedly: the wall check samples triangle centres."""
    for _ in range(levels):
        a, b, c = triangles[:, 0], triangles[:, 1], triangles[:, 2]
        ab, bc, ca = (a + b) / 2, (b + c) / 2, (c + a) / 2
        triangles = np.concatenate([np.stack(t, axis=1) for t in ((a, ab, ca), (ab, b, bc), (ca, bc, c), (ab, bc, ca))])
    return triangles


def wedge(angle_deg, length=4., depth=4.):
    """A prism with one edge of the given angle along Z; outward-wound and finely tessellated."""
    import math
    rise = length * math.tan(math.radians(angle_deg))
    a, b, c = (0, 0), (length, 0), (length, rise)          # The measured edge is at a.
    front = [(x, y, 0.) for x, y in (a, b, c)]
    back = [(x, y, depth) for x, y in (a, b, c)]
    triangles = [[front[0], front[2], front[1]], [back[0], back[1], back[2]]]
    for i, j in ((0, 1), (1, 2), (2, 0)):
        triangles += [[front[i], front[j], back[j]], [front[i], back[j], back[i]]]
    return refined(np.array(triangles, float), 4)


class BendTests(unittest.TestCase):
    def test_a_turn_tighter_than_the_tube_is_reported_with_numbers(self):
        warning, = program_warnings(run_program(TIGHT))
        self.assertEqual(warning['code'], 'TUBE_BEND_TOO_TIGHT')
        self.assertEqual(warning['component'], 'Leg')
        self.assertEqual(warning['between_points'], [1, 2])
        self.assertLess(warning['min_bend_radius_mm'], warning['tube_radius_mm'])
        self.assertAlmostEqual(warning['min_bend_radius_mm'], 3.28, delta=.05)
        # The same path at a smaller scale: the clearance does not scale, the bend gets tighter still.
        scaled, = program_warnings(run_program(TIGHT, {'H': 120}))
        self.assertLess(scaled['min_bend_radius_mm'] / scaled['tube_radius_mm'],
                        warning['min_bend_radius_mm'] / warning['tube_radius_mm'] + 1e-9)

    def test_gentle_and_straight_paths_are_quiet(self):
        self.assertEqual(program_warnings(run_program(GENTLE)), [])
        self.assertEqual(program_warnings(run_program('model.tube("Pin", [(0, 0, 0), (0, 0, 20)], 5)')), [])
        self.assertEqual(bend_radii([(0, 0, 0), (0, 10, 0)]), [float('inf')])

    def test_variable_tube_uses_its_larger_radius(self):
        code = 'model.variable_tube("Horn", [(0, 0, 0), (0, 3, 0), (4, 5, 0), (12, 5, 0)], [4, 4, 1, 1])'
        self.assertEqual(program_warnings(run_program(code))[0]['tube_radius_mm'], 4)

    def test_a_failing_check_never_breaks_the_answer(self):
        with patch('solidworks_mcp.shape_checks.wide_masks', side_effect=RuntimeError('boom')):
            from solidworks_mcp import shape_checks
            with patch.object(shape_checks, 'wide_masks', side_effect=RuntimeError('boom')):
                self.assertEqual([w['code'] for w in shape_checks.program_warnings(run_program(TIGHT))],
                                 ['TUBE_BEND_TOO_TIGHT'])

    def test_analyze_carries_the_warning(self):
        result = dispatch_new_tool(server.sw_automation, 'execute_model_code', {'code': TIGHT})
        self.assertTrue(result['success'])
        self.assertEqual(result['data']['warnings'][0]['code'], 'TUBE_BEND_TOO_TIGHT')
        quiet = dispatch_new_tool(server.sw_automation, 'execute_model_code', {'code': GENTLE})
        self.assertNotIn('warnings', quiet['data'])
        self.assertNotIn('warnings', compact_analysis({'nodes': [], 'warnings': []}))


class WideMaskTests(unittest.TestCase):
    PATCH = ("host = model.revolve('Host', [(0, 10.7), (30, 10.7)])\n"
             "mask = model.rounded_prism('Mask', %s, 7, 14, 1.5, center=(0, 15, 7.5))\n"
             "model.surface_patch('Mouth', host, mask, 3, inward=True)")

    def test_a_mask_almost_as_wide_as_the_host_is_reported(self):
        warning, = program_warnings(run_program(self.PATCH % 17.8))
        self.assertEqual(warning['code'], 'SURFACE_PATCH_WIDE_MASK')
        self.assertEqual((warning['component'], warning['mask']), ('Mouth', 'Mask'))
        self.assertEqual(warning['mask_width_share'], .83)
        self.assertIn('curved_inlay', warning['action'])

    def test_a_narrow_mask_and_the_documented_example_are_quiet(self):
        from solidworks_mcp.automation.shape_service import API
        self.assertEqual(program_warnings(run_program(self.PATCH % 12)), [])
        self.assertEqual(program_warnings(run_program(API['surface_example'])), [])


class DraftTests(unittest.TestCase):
    def test_a_box_over_one_side_is_a_plane_cut(self):
        code = ("ball = model.ellipsoid('Ball', (10.95, 9, 10.95), center=(12.05, 2, 0.15))\n"
                "trim = model.rounded_prism('Trim', 26, 26, 10, 0, center=(12.05, -5, 0.15), axis='Y')\n"
                "model.combine('Foot', [ball, trim], operation='difference')")
        extents = draft_extents(run_program(code))
        self.assertEqual(extents['estimated'], [])
        self.assertEqual(extents['bodies']['Foot'][1], 0.0)             # Cut at the ground, not at -7.
        self.assertAlmostEqual(extents['bodies']['Foot'][4], 11.0, delta=.05)

    def test_other_cutters_keep_the_estimate(self):
        code = ("ball = model.ellipsoid('Ball', (10, 9, 10), center=(0, 2, 0))\n"
                "tool = model.rounded_prism('Tool', 26, 26, 10, 3, center=(0, -5, 0), axis='Y')\n"
                "model.combine('Foot', [ball, tool], operation='difference')")
        self.assertEqual(draft_extents(run_program(code))['estimated'], ['Foot'])

    def test_intersection_keeps_the_shape_inside_the_mask(self):
        shell = "shell = model.sectioned_solid('Shell', [(0, 16, 14), (40, 20, 17)])\n"
        boxed = draft_extents(run_program(shell + "mask = model.rounded_prism('Mask', 20, 20, 30, 0, center=(0, 20, 20))\n"
                                          "model.combine('Panel', [shell, mask], operation='intersection')"))
        self.assertEqual(boxed['estimated'], [])
        self.assertEqual(boxed['bodies']['Panel'], [-10.0, 10.0, 5.0, 10.0, 30.0, 16.25])
        polygon = run_program(shell + "mask = model.prism('Mask', [(-10, 10), (10, 10), (8, 30), (-8, 30)], 30, center=(0, 0, 20))\n"
                              "model.combine('Panel', [shell, mask], operation='intersection')")
        self.assertEqual(draft_extents(polygon)['estimated'], ['Panel'])
        # Low on the taper the panel ends at the shell there, not at the widest radius of the shell.
        pieces = np.vstack(draft_components(polygon)['Panel']['pieces'])
        low = pieces[pieces[:, 1] < 12]
        self.assertLess(low[:, 2].max(), 15.2)

    def test_an_inward_patch_stays_inside_its_host(self):
        code = ("host = model.revolve('Host', [(0, 10), (30, 10)])\n"
                "mask = model.rounded_prism('Mask', 8, 6, 14, 1, center=(0, 15, 7.5))\n"
                "model.surface_patch('Patch', host, mask, 3, inward=True)")
        self.assertLessEqual(draft_extents(run_program(code))['bodies']['Patch'][5], 10.0)
        outward = code.replace('3, inward=True', '3')
        self.assertGreater(draft_extents(run_program(outward))['bodies']['Patch'][5], 10.0)


class AnswerTests(unittest.TestCase):
    def test_export_answer_does_not_collide_with_error_details(self):
        compact = compact_export({'status': 'EXPORT_VALIDATED', 'files': [{'path': 'model.step', 'kind': 'step'}]})
        self.assertNotIn('details', compact)
        self.assertIn('manifest.json', compact['note'])
        self.assertFalse(server._outcome_unknown({'success': True, 'data': compact}))
        self.assertFalse(server._outcome_unknown({'success': True, 'data': {'details': 'free text'}}))
        self.assertTrue(server._outcome_unknown({'data': {'error': {'details': {'native_outcome_unknown': True}}}}))
        self.assertTrue(server._outcome_unknown({'data': {'details': {'native_outcome_unknown': True}}}))
        self.assertFalse(server._outcome_unknown({}))


class RebuildDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        def feature(name, kind, code):
            return SimpleNamespace(Name=name, GetTypeName2=kind, GetErrorCode2=lambda: (code, False))
        self.features = [feature('TorsoCore', 'Blend', 0), feature('LegTubeL', 'Sweep', 1),
                         feature('LegL', 'CombineBodies', 1), feature('LegR', 'MirrorSolid', 1)]
        self.automation = SimpleNamespace(_walk_features=lambda doc: self.features)
        self.ownership = {'LegTubeL': {'features': ['LegTubeL'], 'support': ['LegTubeL_Path1', 'LegTubeL_Section']},
                          'LegL': {'features': ['LegL'], 'support': []}, 'LegR': {'features': ['LegR'], 'support': []}}

    def run_with_typed(self, function, *args):
        with patch('solidworks_mcp.automation.shape_parametric.typed', lambda obj, name: obj):
            return function(*args)

    def test_failures_come_in_tree_order_with_their_components(self):
        failures = self.run_with_typed(rebuild_failures, self.automation, None, self.ownership)
        self.assertEqual([f['feature'] for f in failures], ['LegTubeL', 'LegL', 'LegR'])
        self.assertEqual(failures[0]['type'], 'Sweep')
        self.assertEqual(failures[0]['component'], 'LegTubeL')
        self.assertIn('bends tighter', failures[0]['hint'])
        self.assertNotIn('hint', failures[1])                # Only the cause is explained.
        self.assertEqual(feature_owners(self.ownership)['LegTubeL_Path1'], 'LegTubeL')

    def test_the_error_names_the_cause(self):
        error = self.run_with_typed(rebuild_error, self.automation, None, 'Edited document did not rebuild', self.ownership)
        self.assertIsInstance(error, ProgramError)
        self.assertEqual(error.code, 'NATIVE_REBUILD_FAILED')
        self.assertIn('LegTubeL (Sweep) and 2 dependent feature(s)', str(error))
        self.assertEqual(error.details['component'], 'LegTubeL')
        self.assertEqual(len(error.details['features']), 3)
        self.features[:] = self.features[:1]
        plain = self.run_with_typed(rebuild_error, self.automation, None, 'Edited document did not rebuild', None)
        self.assertEqual(str(plain), 'Edited document did not rebuild')

    def test_actions_point_at_the_component(self):
        details = {'component': 'LegTubeL', 'features': [{'feature': 'LegTubeL', 'component': 'LegTubeL'}]}
        actions = recovery_actions('NATIVE_REBUILD_FAILED', 'execute_model_code', {'document_path': 'D:\\p.SLDPRT'}, details)
        self.assertEqual(actions[0]['arguments'], {'components': ['LegTubeL']})
        self.assertEqual(actions[1]['arguments']['mode'], 'analyze')
        invalid = recovery_actions('NATIVE_BODY_INVALID', 'export_model', {}, {'faults': {'components': ['ShoulderCone']}})
        self.assertEqual(invalid[0]['arguments'], {'components': ['ShoulderCone']})


class KernelFaultTests(unittest.TestCase):
    def test_faulty_faces_are_traced_to_their_components(self):
        from solidworks_mcp.automation import model_inspection
        cone = SimpleNamespace(GetFeature=SimpleNamespace(Name='ShoulderCone_Span2'))
        faults = SimpleNamespace(Count=2, Entity=lambda index: cone)
        bad = SimpleNamespace(Name='Torso', Check3=faults)
        good = SimpleNamespace(Name='Head', Check3=SimpleNamespace(Count=0, Entity=lambda index: None))
        automation = SimpleNamespace(_get_solid_bodies=lambda doc: [good, bad])
        ownership = {'ShoulderCone': {'features': ['ShoulderCone', 'ShoulderCone_Span2'], 'support': []}}
        with patch.object(model_inspection, 'typed', lambda obj, name: obj):
            report = model_inspection.kernel_faults(automation, None, ownership)
        self.assertEqual(report, [{'body': 'Torso', 'fault_count': 2, 'components': ['ShoulderCone']}])


class LoftConnectorTests(unittest.TestCase):
    def section(self, height, rx, rz, first=0, kind='3DProfileFeature'):
        """A section sketch whose four vertices start at an arbitrary quarter."""
        corners = [(rx, height, 0.), (0., height, rz), (-rx, height, 0.), (0., height, -rz)]
        corners = corners[first:] + corners[:first]
        points = [SimpleNamespace(X=x, Y=y, Z=z) for x, y, z in corners]
        return SimpleNamespace(GetTypeName2=kind, Name='S', GetSpecificFeature2=SimpleNamespace(GetSketchPoints2=points))

    def test_every_section_is_picked_at_the_same_angle(self):
        compiler = ParametricCompiler.__new__(ParametricCompiler)
        picks = compiler.connector_points([self.section(.091, .0194, .01595), self.section(.098, .0107, .0107, first=1),
                                           self.section(.105, .008, .008, first=3)])
        self.assertEqual([round(p[0], 4) for p in picks], [.0194, .0107, .008])      # Always the +X vertex.
        self.assertTrue(all(abs(p[2]) < 1e-12 for p in picks))

    def test_sections_in_a_plane_sketch_keep_the_plain_selection(self):
        compiler = ParametricCompiler.__new__(ParametricCompiler)
        self.assertIsNone(compiler.connector_points([self.section(0, 1, 1), self.section(1, 1, 1, kind='ProfileFeature')]))


class WallTests(unittest.TestCase):
    def test_a_blunt_edge_is_not_a_thin_wall(self):
        result = wall_thickness(wedge(50), min_wall_mm=1.2)      # Corners of 50, 90 and 40 degrees.
        self.assertIsNot(result['passed'], False)
        self.assertEqual(result['thin_area_mm2'], 0)
        self.assertGreater(result.get('sharp_edge_area_mm2', 0), 0)

    def test_a_thin_fin_is_located(self):
        mesh = np.vstack([box((-10, 0, -10), (10, 5, 10)), box((-10, 5, -.4), (10, 25, .4))])
        result = wall_thickness(box((-10, 5, -.4), (10, 25, .4)), min_wall_mm=1.2)
        self.assertFalse(result['passed'])
        self.assertEqual(result['min_thickness_mm'], .8)
        self.assertEqual(result['thin_spots'][0]['thickness_mm'], .8)
        self.assertLessEqual(len(result['thin_spots']), 3)
        self.assertEqual(len(mesh), 24)

    def test_a_sliver_fails_and_a_feathered_rim_does_not(self):
        sliver = wall_thickness(wedge(10, length=6, depth=6), min_wall_mm=1.2)    # Under half the wall over a strip.
        self.assertFalse(sliver['passed'])
        self.assertLess(sliver['thin_spots'][0]['thickness_mm'], .6)
        # The rim of a hole in a face that slopes at 38 degrees: thin only at its very edge.
        rim = wall_thickness(wedge(38), min_wall_mm=1.2)
        self.assertIsNot(rim['passed'], False)
        self.assertEqual(rim['thin_area_mm2'], 0)
        self.assertGreater(rim['sharp_edge_area_mm2'], 0)


class CompareToleranceTests(unittest.TestCase):
    def reference(self, folder):
        image = np.full((400, 300), 255, np.uint8)
        image[50:350, 100:200] = 0                   # 20 x 60 mm at 5 px per mm.
        path = str(Path(folder) / 'reference.png')
        Image.fromarray(image).convert('RGB').save(path)
        return path

    def test_an_edge_within_tolerance_does_not_fail_its_level(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.reference(folder)
            # The model has a wide foot whose top is 1 mm lower than drawn: every level still matches nearby.
            near = MeshSilhouette(np.vstack([box((-10, 0, -3), (10, 60, 3))]))
            self.assertEqual(compare_reference(path, near, 'front', height_mm=60, tolerance_mm=1.5)['levels']['failed'], 0)
            stepped = MeshSilhouette(np.vstack([box((-10, 0, -3), (10, 60, 3)), box((-20, 0, -3), (20, 9.0, 3))]))
            close = compare_reference(path, stepped, 'front', height_mm=60, levels=30, tolerance_mm=1.5)
            far = compare_reference(path, MeshSilhouette(np.vstack([box((-10, 0, -3), (10, 60, 3)),
                                                                   box((-20, 0, -3), (20, 9.0, 3))])),
                                    'front', height_mm=60, levels=30, tolerance_mm=.3)
            # Levels 1, 3, 5, 7 mm lie inside the foot; 9 mm is its edge and passes at the wide tolerance only.
            self.assertEqual(close['levels']['failed'], 4)
            self.assertEqual(far['levels']['failed'], 5)
            shifted = [row for row in close['worst'] if 'model_y_mm' in row]
            self.assertEqual(shifted, [])


class ColourQueryTests(unittest.TestCase):
    def test_a_coloured_element_inside_the_figure_is_measured(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Image.new('RGB', (300, 300), (251, 252, 252))
            draw = ImageDraw.Draw(image)
            draw.rectangle((50, 50, 250, 250), fill=(200, 212, 225), outline=(0, 0, 0))
            draw.ellipse((100, 100, 160, 140), fill=(251, 243, 198))             # 60 x 40 px.
            draw.line((60, 200, 240, 200), fill=(251, 243, 198), width=1)        # A hairline of the same colour.
            path = str(Path(folder) / 'figure.png')
            image.save(path)
            query = {'id': 'eye', 'kind': 'bbox', 'region': [55, 55, 245, 245], 'color': [251, 243, 198],
                     'expected_mm': {'width': 12.2, 'height': 8.2}, 'tolerance_mm': .5}
            result = measure_reference(path, [query], projection='front',
                                       calibration={'pixel_distance': 5, 'length_mm': 1})
            eye = result['measurements'][0]
            self.assertEqual(eye['bbox_px'], [100, 100, 161, 141])
            self.assertTrue(eye['check']['passed'])
            plain = measure_reference(path, [{'id': 'all', 'kind': 'bbox', 'region': [55, 55, 245, 245]}])
            self.assertNotEqual(plain['measurements'][0]['bbox_px'], eye['bbox_px'])
            for bad in ({'color': [300, 0, 0]}, {'color': [1, 2]}, {'color_tolerance': 20},
                        {'color': [1, 2, 3], 'color_tolerance': 2}):
                with self.assertRaises(ProgramError):
                    measure_reference(path, [{'id': 'q', 'kind': 'bbox', 'region': [0, 0, 10, 10], **bad}])


if __name__ == '__main__':
    unittest.main()
