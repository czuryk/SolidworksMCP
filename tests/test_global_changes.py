"""Offline checks for shape primitives, orientation, placement and compact responses."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from solidworks_mcp import server
from solidworks_mcp.automation.shape_service import API, SHAPE_INSTRUCTIONS
from solidworks_mcp.automation.sta_executor import StaExecutor
from solidworks_mcp.build_info import changed_files, server_state, source_fingerprint
from solidworks_mcp.session_trace import SessionTrace, load_trace, session_report
from solidworks_mcp.shape_draft import draft_extents
from solidworks_mcp.shape_equations import plan_recipes
from solidworks_mcp.shape_program import ProgramError, run_program
from solidworks_mcp.shape_responses import compact_export, compact_observation
from solidworks_mcp.tool_registry import dispatch_new_tool


def plan(code, legacy=()):
    """legacy: prisms planned with the extrusion recipe of parts saved before the sweep recipe."""
    program = run_program(code, recipe_versions={name: 'prism_extrude_v1' for name in legacy} or None)
    return plan_recipes(program, {k: f'D{i+1}@ShapeParameters' for i, k in enumerate(program['parameters'])})


def ok(**data):
    return {'success': True, 'message': 'fixture', 'error_code': 0, 'error_name': 'swSuccess', 'data': data}


class ConstructorTests(unittest.TestCase):
    """G4: polygon prism, plain box, closed revolve."""

    def test_prism_outline_becomes_straight_native_edges(self):
        code = 'model.prism("L", [(0, 0), (30, 0), (30, 6), (6, 6), (6, 24), (0, 24)], 10, center=(1, 2, 5))'
        swept = plan(code)[0]
        self.assertEqual(swept['variant'], 'sweep')
        self.assertEqual([(s['mode'], len(s['curves']), s.get('role')) for s in swept['sketches']],
                         [('3d', 1, 'path'), ('3d', 6, None)])
        # Nothing of a swept prism enters the equation manager.
        self.assertEqual(swept['placement'], {})
        self.assertNotIn('extrusion', swept)
        self.assertNotIn('dimensions', swept)
        self.assertEqual(swept['sketches'][0]['curves'][0]['xyz'], ['1', '2', '(10*t+0)'])
        spec = plan(code, legacy=['L'])[0]
        self.assertEqual(len(spec['sketches'][0]['curves']), 6)
        self.assertFalse(spec['sketches'][0]['axis'])
        self.assertEqual(spec['extrusion']['value'], 10)
        self.assertEqual(set(spec['placement']), {'Z'})
        self.assertNotIn('variant', spec)
        extents = draft_extents(run_program(
            'model.prism("L", [(0, 0), (30, 0), (30, 6), (6, 6), (6, 24), (0, 24)], 10, center=(1, 2, 5))'))
        self.assertEqual(extents['bbox'], [1.0, 2.0, 0.0, 31.0, 26.0, 10.0])

    def test_prism_axes_use_the_verified_sketch_frames(self):
        swept = plan('model.prism("P", [(0, 0), (10, 0), (0, 4)], 6, center=(0, 20, 0), axis="Y")')[0]
        self.assertEqual(swept['variant'], 'axis=Y|sweep')
        # b runs along +Z in model space; the profile sits at the low end of the Y axis.
        self.assertEqual([c['xyz'][1] for c in swept['sketches'][1]['curves']], ['17'] * 3)
        self.assertIn('4', ''.join(c['xyz'][2] for c in swept['sketches'][1]['curves']))
        top = plan('model.prism("P", [(0, 0), (10, 0), (0, 4)], 6, center=(0, 20, 0), axis="Y")', legacy=['P'])[0]
        self.assertEqual(top['variant'], 'axis=Y')
        self.assertEqual(top['sketches'][0]['plane'], 'Top')
        self.assertEqual(set(top['placement']), {'Y'})
        # b runs along +Z, which is -y in the Top sketch: the outline is not mirrored.
        self.assertIn('-4', ''.join(c['xyz'][1] for c in top['sketches'][0]['curves']))
        box = draft_extents(run_program('model.prism("P", [(0, 0), (10, 0), (0, 4)], 6, center=(0, 20, 0), axis="Y")'))
        self.assertEqual(box['bbox'], [0.0, 17.0, 0.0, 10.0, 23.0, 4.0])
        side = plan('model.prism("P", [(0, 0), (10, 0), (0, 4)], 6, axis="X")', legacy=['P'])[0]
        self.assertEqual(side['sketches'][0]['plane'], 'Right')

    def test_bad_outlines_are_rejected_before_cad(self):
        for points in ('[(0, 0), (10, 0)]', '[(0, 0), (10, 0), (10, 0)]', '[(0, 0), (10, 0), (20, 0)]',
                       '[(0, 0), (10, 10), (10, 0), (0, 10), (-5, 5)]'):
            with self.subTest(points=points), self.assertRaises(ProgramError) as raised:
                run_program(f'model.prism("P", {points}, 5)')
            self.assertEqual(raised.exception.code, 'INVALID_PROFILE')
        with self.assertRaises(ProgramError):
            run_program('model.prism("P", [(0, 0), (10, 0), (0, 4)], 0)')

    def test_zero_radius_is_a_box(self):
        swept = plan('model.rounded_prism("Box", 20, 10, 6, 0, center=(0, 0, 3))')[0]
        self.assertEqual(swept['variant'], 'box|sweep')
        self.assertEqual(len(swept['sketches'][1]['curves']), 4)
        spec = plan('model.rounded_prism("Box", 20, 10, 6, 0, center=(0, 0, 3))', legacy=['Box'])[0]
        self.assertEqual(spec['variant'], 'box')
        self.assertEqual(len(spec['sketches'][0]['curves']), 4)
        with self.assertRaises(ProgramError):
            plan('H = model.parameter("H", 10)\nmodel.rounded_prism("Box", 20, 10, 6, H - 10)')
        with self.assertRaises(ProgramError):
            run_program('model.rounded_prism("Box", 20, 10, 6, -1)')

    def test_closed_revolve_is_a_ring(self):
        ring = plan('model.revolve("Ring", [(0, 8), (4, 8), (4, 10), (0, 10)], closed=True)')[0]
        self.assertEqual(ring['variant'], 'closed')
        self.assertEqual(len(ring['sketches'][0]['curves']), 4)
        solid = plan('model.revolve("Disc", [(0, 8), (4, 8)])')[0]
        self.assertNotIn('variant', solid)
        self.assertEqual(len(solid['sketches'][0]['curves']), 4)   # Closed through the axis.
        with self.assertRaises(ProgramError):
            run_program('model.revolve("Ring", [(0, 8), (4, 8)], closed=True)')
        with self.assertRaises(ProgramError):
            run_program('model.revolve("Ring", [(0, 8), (4, 10), (4, 8), (0, 10)], closed=True)')


class GuidanceTests(unittest.TestCase):
    """G5: the server explains its own workflow to any client."""

    def test_instructions_are_a_short_complete_workflow(self):
        self.assertLessEqual(len(SHAPE_INSTRUCTIONS), 1500)
        for word in ('get_model_context', 'model_api_lookup', 'analyze', 'draft_views', 'compare_reference',
                     'create', 'patch', 'revert', 'inspect_model', 'export_model', 'wait_operation',
                     'recommended_actions'):
            self.assertIn(word, SHAPE_INSTRUCTIONS)
        tools = {tool.name for tool in asyncio.run(server.list_tools())}
        for name in ('compare_reference', 'wait_operation', 'execute_model_code'):
            self.assertIn(name, tools)

    def test_every_example_is_a_valid_program(self):
        checked = 0
        for name, code in API['examples'].items():
            if code.startswith('see topic'):
                continue
            with self.subTest(example=name):
                self.assertTrue(plan(code))
                self.assertTrue(draft_extents(run_program(code))['bodies'])
                checked += 1
        self.assertGreaterEqual(checked, 14)
        self.assertTrue(plan(API['surface_example']))
        self.assertTrue(plan(API['example']))

    def test_unknown_topic_suggests_the_closest(self):
        for typo, expected in (('pitfals', 'pitfalls'), ('rounded_prizm', 'rounded_prism'), ('workflw', 'workflow')):
            result = server.sw_automation.model_api_lookup(typo)
            self.assertFalse(result['success'])
            error = result['data']['error']
            self.assertEqual(error['details']['did_you_mean'][0], expected)
            self.assertEqual(error['recommended_actions'][0]['arguments'], {'topic': expected})
        index = server.sw_automation.model_api_lookup('index')['data']
        for topic in ('workflow', 'pitfalls', 'examples', 'all'):
            self.assertIn(topic, index['topics'])
        self.assertIn('Build once', server.sw_automation.model_api_lookup('workflow')['data']['workflow'])


class ServerStateTests(unittest.TestCase):
    """G6: a process left behind by source edits says so."""

    def test_changed_source_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'a.py').write_text('x = 1')
            (root / 'sub').mkdir()
            (root / 'sub' / 'b.py').write_text('y = 2')
            baseline = source_fingerprint(root)
            self.assertEqual(changed_files(baseline, root), [])
            self.assertFalse(server_state(baseline, root)['code_changed_since_start'])
            later = time.time() + 5
            (root / 'sub' / 'b.py').write_text('y = 3')
            os.utime(root / 'sub' / 'b.py', (later, later))
            (root / 'c.py').write_text('z = 1')
            state = server_state(baseline, root)
            self.assertTrue(state['code_changed_since_start'])
            self.assertEqual(state['changed_files'], ['c.py', 'sub/b.py'])
            self.assertIn('reconnect', state['action'])

    def test_status_carries_the_server_state(self):
        result = asyncio.run(server.call_tool('get_executor_status', {}))
        state = result.structuredContent['data']['server']
        self.assertEqual(state['pid'], os.getpid())
        self.assertIn('code_changed_since_start', state)


class TraceTests(unittest.TestCase):
    """G7: local session trace and report."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.trace = SessionTrace(self.temp.name)
        self.executor = StaExecutor(Path(self.temp.name) / 'session.lock', identity_provider=lambda: [],
                                    initialize=lambda: None, finalize=lambda: None, pump=lambda: None)
        self.addCleanup(self.executor.close)

    def call(self, name, arguments, direct=None):
        with patch.object(server, 'session_trace', self.trace), \
             patch.object(server, 'cad_executor', self.executor), \
             patch.object(server, '_call_tool_direct', return_value=direct or ok()), \
             patch.object(server, 'request_spec', return_value=None), \
             patch.object(server, '_execute_cad_request', side_effect=lambda n, a: direct or ok()):
            return asyncio.run(server.call_tool(name, arguments))

    def test_calls_are_traced_without_program_text(self):
        secret = 'model.dome("SecretName", 4, 2)'
        self.call('execute_model_code', {'code': secret})
        self.call('execute_model_code', {'mode': 'edit', 'document_path': 'D:\\p.SLDPRT', 'expected_revision': 'r',
                                         'patch': [{'old': 'SecretOld', 'new': 'SecretNew'}]},
                  ok(status='NATIVE_GEOMETRY_UPDATED', timings_ms={'total': 61000}))
        self.call('execute_model_code', {'mode': 'edit', 'document_path': 'D:\\p.SLDPRT', 'expected_revision': 'r',
                                         'parameters': {'H': 120}},
                  {'success': False, 'message': 'stale', 'error_code': -1, 'error_name': 'STALE_REVISION', 'data': {}})
        self.call('expert_tools', {'action': 'call', 'name': 'get_document_info'})
        self.call('model_api_lookup', {'topic': 'all'})
        self.call('wait_operation', {'operation_id': 'missing'})
        report = session_report(self.trace.snapshot())
        self.assertEqual(report['calls'], 6)
        self.assertEqual(report['calls_by_tool']['execute_model_code'], 3)
        self.assertEqual(report['calls_by_tool']['get_document_info'], 1)
        self.assertNotIn('expert_tools', report['calls_by_tool'])
        self.assertEqual((report['mutations_create_edit'], report['mutations_failed']), (2, 1))
        self.assertEqual((report['patch_edits'], report['parameter_only_edits'], report['analyze_calls']), (1, 1, 1))
        self.assertEqual(report['errors_by_code']['STALE_REVISION'], 1)
        self.assertEqual(report['api_lookups'], 1)
        self.assertEqual(report['recovery_calls'], 0)
        self.assertEqual(report['wait_operation_calls'], 1)
        self.assertEqual(report['input_chars_code'], len(secret))
        self.assertEqual(report['input_chars_patch'], len('SecretOld') + len('SecretNew'))
        self.assertEqual(report['slowest_operations'][0]['seconds'], 61.0)
        self.assertGreater(report['result_chars_total'], 0)
        text = self.trace.path.read_text(encoding='utf-8')
        self.assertNotIn('Secret', text)
        self.assertEqual(len(load_trace(self.trace.path)), 6)

    def test_report_tool_reads_the_trace(self):
        self.call('model_api_lookup', {'topic': 'index'})
        result = self.call('expert_tools', {'action': 'call', 'name': 'get_session_report'})
        data = result.structuredContent['data']
        self.assertEqual(data['api_lookups'], 1)
        self.assertEqual(data['trace_file'], str(self.trace.path))
        self.assertIn('server', data)

    def test_running_answers_are_counted(self):
        calls = [{'tool': 'execute_model_code', 'mode': 'create', 'running': True, 'result_chars': 200,
                  'duration_ms': 45000, 'success': True},
                 {'tool': 'wait_operation', 'result_chars': 2500, 'duration_ms': 30000, 'success': True}]
        report = session_report(calls)
        self.assertEqual(report['mutations_answered_running'], 1)
        self.assertEqual(report['wait_operation_calls'], 1)
        self.assertEqual(report['seconds_by_tool']['execute_model_code'], 45.0)


class CompactAnswerTests(unittest.TestCase):
    """G8: export and observation answers."""

    BODIES = ['LegL', 'Torso', 'EyeR', 'Head', 'LegR', 'Teeth', 'ArmR', 'ArmL', 'EyeL']

    def test_export_answer_points_to_the_manifest(self):
        inspection = {'passed': True, 'triangles': 18234, 'vertices': 9119, 'boundary_edges': 0,
                      'nonmanifold_edges': 0, 'signed_volume_mm3': 1234.5, 'native_volume_mm3': 1234.6,
                      'relative_volume_error': 1e-5, 'bbox_max_delta_mm': .001, 'self_intersections': 'NOT_CHECKED'}
        files = [{'path': 'model.SLDPRT', 'kind': 'sldprt', 'sha256': 'a' * 64, 'size_bytes': 4248595,
                  'inspection': {'container': 'OLE', 'streams': 30}},
                 {'path': 'model.step', 'kind': 'step', 'sha256': 'b' * 64, 'size_bytes': 3000000,
                  'inspection': {'solid_body_count': 9, 'closed_shell_count': 9, 'unit': 'mm'}}]
        files += [{'path': f'stl/{name}.stl', 'kind': 'stl', 'sha256': 'c' * 64, 'size_bytes': 911784, 'body': name,
                   'inspection': inspection, 'tessellation': {'deviation_mm': .02, 'angle_deg': 10}}
                  for name in self.BODIES]
        full = {'status': 'EXPORT_VALIDATED', 'directory': 'D:\\out', 'manifest': 'D:\\out\\manifest.json',
                'manifest_sha256': 'd' * 64, 'files': files, 'document_path': 'D:\\p.SLDPRT', 'revision': 'e' * 64,
                'acceptance': {'status': 'EXPORT_VALIDATED', 'native_kernel': 'PASS', 'mesh_structure': 'PASS',
                               'shape_likeness': 'NOT_CHECKED', 'wall_thickness': 'NOT_CHECKED'},
                'source_preserved': True, 'replaced_previous_delivery': False, 'timings_ms': {'total': 90000}}
        self.assertGreater(len(json.dumps(full)), 5000)
        answer = compact_export(full)
        self.assertLessEqual(len(json.dumps(ok(**answer), separators=(',', ':'))), 3000)
        self.assertEqual(len(answer['files']), 11)
        self.assertEqual(answer['files'][2], {'path': 'stl/LegL.stl', 'kind': 'stl', 'body': 'LegL',
                                              'size_bytes': 911784, 'triangles': 18234})
        self.assertEqual(answer['acceptance']['mesh_structure'], 'PASS')

    def test_observation_answer_keeps_images_and_short_body_facts(self):
        body = {'name': 'Head', 'visible': True, 'face_count': 42,
                'bbox': {'min': [-13.9, 96.9933, -10.75], 'max': [13.9, 150.0, 15.8]},
                'volume': 12345.678, 'volume_exact': 12345.678901, 'surface_area': 5432.1,
                'center_of_mass': [0, 120.5, 1.2], 'dimensions_mm': [27.8, 53.0067, 26.55],
                'components': [{'name': 'Head', 'id': 'x' * 32 + ':Head', 'declared_recipe': 'combine',
                                'native_feature_type': 'Combine'}],
                'surface_types': {'plane': 6, 'cylinder': 20, 'bspline': 16}}
        image = {'view': 'front', 'path': 'D:\\work\\Assembly_views\\' + 'c' * 32 + '\\front.png',
                 'document': 'D:\\work\\Assembly.SLDPRT', 'revision': 'a' * 64, 'configuration': 'Default',
                 'size_px': [1000, 1000], 'sha256': 'd' * 64, 'native_scale': 1.5, 'perspective': False}
        full = {'document': 'D:\\work\\Assembly.SLDPRT', 'revision': 'a' * 64, 'configuration': 'Default',
                'update_stamp': 771, 'components': None, 'bodies': [dict(body, name=name) for name in self.BODIES],
                'directory': 'D:\\work\\Assembly_views\\' + 'c' * 32,
                'bbox_mm': {'min': [-31.1, 0, -16.007], 'max': [31.1, 150, 17.432]},
                'bbox_method': 'native_approximate_GetBodyBox',
                'images': [dict(image, view=view) for view in ('front', 'right', 'isometric')], 'sections': [],
                'camera_restored': True, 'timings_sec': {'target_and_facts': 1.2, 'camera_capture_restore': 3.4},
                'framing': 'shared_native_scale_bounding_sphere_with_context',
                'frame_sphere_mm': {'center': [0, 75, 0.7], 'radius': 83.2, 'margin': .12},
                'acceptance': 'OBSERVED_NOT_SHAPE_ACCEPTED'}
        answer = compact_observation(full)
        self.assertLessEqual(len(json.dumps(ok(**answer), separators=(',', ':'))), 2500)
        self.assertEqual([i['view'] for i in answer['images']], ['front', 'right', 'isometric'])
        self.assertEqual(answer['images'][0]['sha256'], 'd' * 64)
        self.assertEqual(answer['bodies'][3]['bbox']['min'], [-13.9, 97, -10.8])
        self.assertNotIn('volume', answer['bodies'][0])
        self.assertNotIn('frame_sphere_mm', answer)

    def test_dispatch_applies_them_unless_verbose(self):
        full = ok(status='EXPORT_VALIDATED', files=[{'path': 'model.step', 'kind': 'step', 'sha256': 'b' * 64,
                                                      'size_bytes': 1, 'inspection': {'unit': 'mm'}}])
        def export_model(document_path, expected_revision, directory):
            return full
        with patch.object(server.sw_automation, 'export_model', export_model):
            compact = dispatch_new_tool(server.sw_automation, 'export_model',
                                        {'document_path': 'p', 'expected_revision': 'r', 'directory': 'd'})
            verbose = dispatch_new_tool(server.sw_automation, 'export_model',
                                        {'document_path': 'p', 'expected_revision': 'r', 'directory': 'd',
                                         'verbose': True})
        self.assertNotIn('inspection', compact['data']['files'][0])
        self.assertIn('inspection', verbose['data']['files'][0])

    def test_observe_argument_selects_views_after_a_mutation(self):
        properties = next(t for t in asyncio.run(server.list_tools())
                          if t.name == 'execute_model_code').inputSchema['properties']
        self.assertEqual(len(properties['observe']['oneOf']), 2)
        import jsonschema
        for value in (True, False, ['front'], [{'name': 'q', 'direction': [0, 0, -1]}]):
            jsonschema.validate({'observe': value}, {'type': 'object', 'properties': {'observe': properties['observe']}})


if __name__ == '__main__':
    unittest.main()
