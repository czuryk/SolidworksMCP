"""Offline checks of the tessellation-based silhouette, support and wall measurements."""
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from solidworks_mcp import server
from solidworks_mcp.automation.model_inspection import printability_options
from solidworks_mcp.reference_compare import compare_reference
from solidworks_mcp.tool_registry import MUTATING_TOOLS, dispatch_new_tool
from solidworks_mcp.mesh_checks import (MeshSilhouette, printability, support, volume_and_centroid,
                                        wall_thickness)
from solidworks_mcp.shape_program import ProgramError


def box(low, high):
    """Closed, outward-wound triangles of an axis-aligned box."""
    (x0, y0, z0), (x1, y1, z1) = low, high
    p = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    quads = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (2, 3, 7, 6), (1, 2, 6, 5), (0, 4, 7, 3)]
    return np.array([[p[a], p[b], p[c]] for q in quads for a, b, c in ((q[0], q[1], q[2]), (q[0], q[2], q[3]))], float)


def inverted(triangles):
    return triangles[:, ::-1]


class MeshCheckTests(unittest.TestCase):
    def test_volume_and_centroid_of_a_box(self):
        volume, centroid = volume_and_centroid(box((0, 0, 0), (10, 20, 30)))
        self.assertAlmostEqual(volume, 6000)
        np.testing.assert_allclose(centroid, [5, 10, 15])

    def test_silhouette_matches_the_projection(self):
        outline = MeshSilhouette(box((-5, 0, -2), (5, 20, 2)))
        self.assertEqual(tuple(round(v, 6) for v in outline.bounds('front')), (-5, 0, 5, 20))
        self.assertEqual(tuple(round(v, 6) for v in outline.bounds('right')), (-2, 0, 2, 20))
        frame = {'px_per_mm': 4, 'origin_px': [50, 90]}
        mask = outline.mask('front', frame, (100, 100))
        self.assertTrue(mask[50, 50])
        self.assertFalse(mask[50, 5])
        self.assertAlmostEqual(mask.sum() / 16, 200, delta=12)     # 10 x 20 mm at 4 px/mm.

    def test_a_box_stands_on_its_base(self):
        result = support({'Body': box((-10, 0, -5), (10, 30, 5))})
        self.assertTrue(result['stable'])
        self.assertEqual(result['plate_mm'], 0)
        self.assertEqual(result['contact_area_mm2'], 200)
        self.assertEqual(result['margin_mm'], 5)
        self.assertEqual(result['footprint_mm'], {'X': [-10, 10], 'Z': [-5, 5]})
        self.assertEqual(result['tip_angle_deg'], 18.4)            # atan(5 / 15)

    def test_an_overhanging_mass_tips_over(self):
        # A small foot under a long arm: the centre of mass is outside the foot.
        result = support({'Foot': box((0, 0, 0), (4, 10, 4)), 'Arm': box((0, 10, 0), (60, 14, 4))})
        self.assertFalse(result['stable'])
        self.assertEqual(result['reason'], 'CENTER_OF_MASS_OUTSIDE_FOOTPRINT')
        self.assertLess(result['margin_mm'], 0)
        self.assertEqual(result['resting_bodies'], ['Foot'])

    def test_a_body_without_a_flat_base_is_reported(self):
        tetra = np.array([[(0, 0, 0), (1, 5, 0), (5, 5, 0)], [(0, 0, 0), (5, 5, 0), (3, 5, 4)],
                          [(0, 0, 0), (3, 5, 4), (1, 5, 0)], [(1, 5, 0), (3, 5, 4), (5, 5, 0)]], float)
        result = support({'Tip': tetra})
        self.assertFalse(result['stable'])
        self.assertEqual(result['reason'], 'NO_FLAT_SUPPORT')

    def test_other_up_axes(self):
        result = support({'Body': box((0, 0, 0), (10, 20, 30))}, up='Z')
        self.assertEqual(result['footprint_mm'], {'X': [0, 10], 'Y': [0, 20]})
        with self.assertRaises(ProgramError):
            support({'Body': box((0, 0, 0), (1, 1, 1))}, up='W')

    def test_wall_of_a_solid_box_is_its_smallest_side(self):
        result = wall_thickness(box((0, 0, 0), (10, 20, 3)), min_wall_mm=1.2)
        self.assertEqual(result['min_thickness_mm'], 3)
        self.assertTrue(result['passed'])
        self.assertEqual(result['thin_area_mm2'], 0)

    def test_thin_plate_fails(self):
        result = wall_thickness(box((0, 0, 0), (10, 20, .8)), min_wall_mm=1.2)
        self.assertEqual(result['min_thickness_mm'], .8)
        self.assertFalse(result['passed'])
        self.assertGreater(result['thin_area_percent'], 80)

    def test_hollow_box_reports_its_shell(self):
        shell = np.vstack([box((0, 0, 0), (20, 20, 20)), inverted(box((1, 1, 1), (19, 19, 19)))])
        result = wall_thickness(shell, min_wall_mm=1.2)
        self.assertEqual(result['min_thickness_mm'], 1)
        self.assertFalse(result['passed'])
        volume, _ = volume_and_centroid(shell)
        self.assertAlmostEqual(volume, 8000 - 5832)

    def test_printability_combines_both(self):
        result = printability({'Base': box((-10, 0, -10), (10, 5, 10)), 'Fin': box((-10, 5, -.4), (10, 25, .4))},
                              min_wall_mm=1.2)
        self.assertTrue(result['support']['stable'])
        self.assertEqual([w['passed'] for w in result['walls']], [True, False])
        self.assertFalse(result['walls_passed'])
        with self.assertRaises(ProgramError):
            printability({'Base': box((0, 0, 0), (1, 1, 1))}, min_wall_mm=0)


class NativeComparisonTests(unittest.TestCase):
    def test_mesh_silhouette_is_compared_like_a_draft(self):
        with tempfile.TemporaryDirectory() as folder:
            # Reference: a 20 x 40 mm black rectangle, 5 px per mm, on white.
            image = np.full((300, 300), 255, np.uint8)
            image[50:250, 100:200] = 0
            path = str(Path(folder) / 'reference.png')
            Image.fromarray(image).convert('RGB').save(path)
            exact = compare_reference(path, MeshSilhouette(box((-10, 0, -3), (10, 40, 3))), 'front',
                                      height_mm=40, directory=folder)
            self.assertEqual(exact['source'], 'native')
            self.assertGreater(exact['iou'], .97)
            self.assertEqual(exact['levels']['failed'], 0)
            self.assertIn('native tessellation', exact['method'])
            wide = compare_reference(path, MeshSilhouette(box((-14, 0, -3), (14, 40, 3))), 'front', height_mm=40)
            self.assertEqual(wide['delta_size_mm'], [8.0, 0.0])
            self.assertEqual(wide['levels']['passed'], 0)
            side = compare_reference(path, MeshSilhouette(box((-10, 0, -3), (10, 40, 3))), 'right', height_mm=40)
            self.assertEqual(side['model_size_mm'], [6.0, 40.0])

    def test_native_source_goes_through_the_cad_queue(self):
        self.assertTrue(server._is_com_free('compare_reference', {'code': 'x'}))
        self.assertTrue(server._is_com_free('compare_reference', {'document_path': 'C:/p.SLDPRT', 'source': 'draft'}))
        self.assertFalse(server._is_com_free('compare_reference', {'document_path': 'C:/p.SLDPRT', 'source': 'native'}))
        self.assertTrue(server._is_com_free('execute_model_code', {'mode': 'analyze'}))
        self.assertFalse(server._is_com_free('execute_model_code', {'mode': 'edit'}))
        self.assertFalse(server._is_com_free('inspect_model', {}))
        self.assertNotIn('compare_reference', MUTATING_TOOLS)

    def test_native_source_needs_a_saved_part(self):
        for arguments in ({'code': 'model.dome("D", 5, 5)', 'source': 'native'},
                          {'document_path': 'relative.SLDPRT', 'source': 'native'},
                          {'document_path': 'C:/p.SLDPRT', 'source': 'native', 'parameters': {'H': 1}},
                          {'code': 'model.dome("D", 5, 5)', 'source': 'mesh'}):
            result = dispatch_new_tool(server.sw_automation, 'compare_reference',
                                       {'image_path': 'C:/missing.png', **arguments})
            self.assertFalse(result['success'], arguments)
            self.assertIn(result['data']['error']['code'], ('INVALID_ARGUMENT', 'INVALID_PATH'), result)

    def test_printability_request_forms(self):
        self.assertIsNone(printability_options(None))
        self.assertIsNone(printability_options(False))
        self.assertEqual(printability_options(True), {'min_wall_mm': 1.2, 'up': 'Y'})
        self.assertEqual(printability_options({'min_wall_mm': .8}), {'min_wall_mm': .8, 'up': 'Y'})
        with self.assertRaises(ProgramError):
            printability_options({'wall': 1})


if __name__ == '__main__':
    unittest.main()
