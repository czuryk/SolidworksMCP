"""Contracts of shape compilation, isolation, and multimodal MCP delivery."""

import asyncio
import base64
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.automation.session_lease import SessionBusy, SessionLease
from solidworks_mcp.server import call_tool, tool_result_content, list_tools
from solidworks_mcp.shape_program import ProgramError, run_program
from solidworks_mcp.shape_tools import SHAPE_TOOL_NAMES
from solidworks_mcp.tool_registry import dispatch_new_tool


PROGRAM = """
H = model.parameter('H', 100)
def cap(name, x):
    return model.dome(name, .10*H, .08*H, center=(x, 0, 0))
for i in range(2):
    cap('Cap'+str(i), (i+1)*.3*H)
"""


class ProgramTests(unittest.TestCase):
    def test_functions_loops_and_parameter_overrides(self):
        result = run_program(PROGRAM, {'H': 150})
        self.assertEqual([n['geometry']['radius'] for n in result['nodes']], [15, 15])
        self.assertEqual(result['nodes'][1]['geometry']['center'], [90, 0, 0])
        self.assertFalse(result['native_parameter_bindings'])
        self.assertIn('parameter', json.dumps(result['source_graph']))
        self.assertNotEqual(result['program_hash'], run_program(PROGRAM)['program_hash'])

    def test_component_endpoints_keep_expression_dependencies(self):
        result = run_program("""
H = model.parameter('H', 100)
t = model.tube('Pipe', [(0,0,0),(H/10,H/2,0)], H/25)
model.dome('Cap', 10, 5, center=t.end)
model.mirror('Pair', [t, 'Cap'])
""", {'H': 200})
        self.assertEqual(result['nodes'][1]['geometry']['center'], [20, 100, 0])
        self.assertEqual(result['nodes'][2]['geometry']['seeds'], ['Pipe', 'Cap'])

    def test_math_and_value_conditions(self):
        result = run_program("""
H = model.parameter('H', 100)
if model.value(H) > 50:
    model.dome('Cap', sqrt(H), (H/20)*cos(0), center=(sin(pi/2),0,0))
print('Shape created')
""")
        self.assertEqual(result['nodes'][0]['geometry']['radius'], 10)
        self.assertAlmostEqual(result['nodes'][0]['geometry']['center'][0], 1)
        self.assertEqual(result['stdout'], 'Shape created\n')

    def test_invalid_geometry_rejected_before_cad(self):
        cases = [
            ("model.tube('P', [(0,0,0),(0,0,0)], 2)", 'DEGENERATE_PATH'),
            ("model.tube('P', [(0,0,0),(0,2,0)], -1)", 'INVALID_RADIUS'),
            ("model.dome('D', 2, 3)", 'INVALID_DOME'),
            ("model.sectioned_solid('S', [(2,4,3),(1,5,4)])", 'SECTION_ORDER'),
            ("model.mirror('M', ['Missing'])", 'UNKNOWN_COMPONENT'),
            ("model.dome('D', 2, 1)\nmodel.dome('D', 2, 1)", 'DUPLICATE_COMPONENT'),
            ("pass", 'EMPTY_PROGRAM'),
        ]
        for code, expected in cases:
            with self.subTest(expected=expected), self.assertRaises(ProgramError) as error:
                run_program(code)
            self.assertEqual(error.exception.code, expected)

    def test_unknown_override_and_symbolic_condition_rejected(self):
        with self.assertRaises(ProgramError) as error:
            run_program(PROGRAM, {'Typo': 150})
        self.assertEqual(error.exception.code, 'UNKNOWN_PARAMETER')
        with self.assertRaises(ProgramError) as error:
            run_program("H=model.parameter('H',100)\nif H: pass")
        self.assertEqual(error.exception.code, 'SYMBOLIC_CONDITION')

    def test_accidental_io_and_unbounded_syntax_guards(self):
        for code in ['import os', 'while True: pass', 'model.__class__', '_x=1']:
            with self.subTest(code=code), self.assertRaises(ProgramError):
                run_program(code)

    def test_loop_budget_and_trace_restoration(self):
        old = sys.gettrace()
        with self.assertRaises(ProgramError) as error:
            run_program('for i in range(1000):\n for j in range(1000):\n  x=i+j')
        self.assertEqual(error.exception.code, 'PROGRAM_LIMIT')
        self.assertIs(sys.gettrace(), old)

    def test_failed_validation_never_connects_to_solidworks(self):
        sw = SolidWorksAutomation()
        with patch.object(sw, 'connect', side_effect=AssertionError('COM accessed')):
            result = sw.execute_model_code('pass', mode='create', output_path='C:/never.SLDPRT')
        self.assertFalse(result['success'])

    def test_existing_output_preserved_before_connection(self):
        sw = SolidWorksAutomation()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'existing.SLDPRT'
            path.write_bytes(b'original')
            with patch.object(sw, 'connect', side_effect=AssertionError('COM accessed')):
                result = sw.execute_model_code(PROGRAM, mode='create', output_path=str(path))
            self.assertFalse(result['success'])
            self.assertEqual(path.read_bytes(), b'original')


class IntegrationBoundaryTests(unittest.TestCase):
    def test_analyze_mcp_is_structured_and_com_free(self):
        with patch('solidworks_mcp.server.sw_automation._document_snapshot',
                   side_effect=AssertionError('COM accessed')):
            result = asyncio.run(call_tool('execute_model_code', {'code': PROGRAM}))
        self.assertFalse(result.isError)
        self.assertEqual(result.structuredContent['data']['status'], 'PROGRAM_VALIDATED')

    def test_strict_arguments_and_tool_schemas(self):
        result = dispatch_new_tool(SolidWorksAutomation(), 'execute_model_code',
                                   {'code': PROGRAM, 'merge_typo': True})
        self.assertFalse(result['success'])
        tools = asyncio.run(list_tools())
        self.assertEqual({t.name for t in tools[:len(SHAPE_TOOL_NAMES)]}, SHAPE_TOOL_NAMES)
        for tool in tools[:len(SHAPE_TOOL_NAMES)]:
            self.assertIs(tool.inputSchema['additionalProperties'], False)
            self.assertNotIn('ui_guard', tool.inputSchema['properties'])

    def test_inline_image_contains_real_pixels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'image.png'
            Image.new('RGB', (1600, 800), (35, 70, 140)).save(path)
            result = tool_result_content('observe_model', {
                'success': True, 'message': 'test',
                'data': {'images': [{'view': 'front', 'path': str(path)}]}})
            frame = next(c for c in result.content if c.type == 'image')
            with Image.open(io.BytesIO(base64.b64decode(frame.data))) as img:
                self.assertEqual(img.size, (1400, 700))
                self.assertEqual(img.getpixel((100,100)), (35,70,140))
            legacy = tool_result_content('take_screenshot', {
                'success': True, 'message': 'test',
                'data': {'path': str(path), 'frame_unreadable': True}})
            self.assertFalse(any(c.type == 'image' for c in legacy))

    def test_screenshot_size_applies_without_compression(self):
        sw = SolidWorksAutomation()
        doc = SimpleNamespace(GraphicsRedraw2=lambda: None)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'image.png'
            def capture(*args):
                Image.new('RGB', (1800, 1200), 'gray').save(path)
                return {'success': True, 'data': {'path': str(path)}}
            with patch.object(sw, 'get_active_doc', return_value=(doc, None)), \
                    patch.object(sw, '_capture_model_image', side_effect=capture):
                result = sw.take_screenshot(str(path), zoom_to_fit=False,
                                            compress=False, width=600, height=600)
            self.assertTrue(result['success'])
            with Image.open(path) as img:
                self.assertEqual(img.size, (600, 400))

    def test_writer_lease_excludes_another_process_and_releases_after_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'session.lock'
            script = (
                'import sys\n'
                'from solidworks_mcp.automation.session_lease import SessionLease, SessionBusy\n'
                'try:\n with SessionLease(sys.argv[1]): pass\n'
                'except SessionBusy:\n sys.exit(23)\n')
            try:
                with SessionLease(path):
                    run = subprocess.run([sys.executable, '-c', script, str(path)],
                                         capture_output=True, timeout=15)
                    self.assertEqual(run.returncode, 23, run.stderr.decode(errors='replace'))
                    raise RuntimeError('simulated failure')
            except RuntimeError:
                pass
            with SessionLease(path):
                pass


class EllipseRegressionTests(unittest.TestCase):
    def test_model_center_and_local_radii_do_not_mix_frames(self):
        sw = SolidWorksAutomation()
        manager = SimpleNamespace(CreateEllipse=Mock(return_value=SimpleNamespace()))
        doc = SimpleNamespace(SketchManager=manager)
        with patch.object(sw, '_to_sketch_m', return_value=(.03, -.02, 0)) as transform:
            sw._create_entity(doc, {'type':'ellipse', 'center':[20,50,30],
                                   'major_radius':10, 'minor_radius':5}, 'mm')
        transform.assert_called_once_with(doc, [20,50,30], 'mm')
        actual = manager.CreateEllipse.call_args.args
        expected = (.03,-.02,0,.04,-.02,0,.03,-.015,0)
        for a,b in zip(actual, expected):
            self.assertAlmostEqual(a,b)

    def test_mixed_or_partial_axis_coordinates_rejected(self):
        sw = SolidWorksAutomation()
        doc = SimpleNamespace(SketchManager=Mock())
        for points in [{'major_point':[1,2]},
                       {'major_point':[1,2], 'minor_point':[2,1]}]:
            with patch.object(sw, '_to_sketch_m', return_value=(0,0,0)), \
                    self.assertRaises(ValueError):
                sw._create_entity(doc, {'type':'ellipse', 'center':[0,0,0], **points}, 'mm')
        doc.SketchManager.CreateEllipse.assert_not_called()


if __name__ == '__main__':
    unittest.main()
