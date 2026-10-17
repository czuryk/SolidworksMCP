import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solidworks_mcp import server
from solidworks_mcp.shape_program import run_program
from solidworks_mcp.shape_responses import (compact_response, compact_context, compact_mutation,
                                            compact_analysis, compact_inspection, rounded)
from solidworks_mcp.automation.sta_executor import StaExecutor
from solidworks_mcp.tool_registry import dispatch_new_tool

FIXTURE = Path(__file__).parent / 'fixtures' / 'bender_program.py'
BODIES = ['LegL', 'Torso', 'EyeR', 'Head', 'LegR', 'Teeth', 'ArmR', 'ArmL', 'EyeL']


def size(value):
    return len(json.dumps(value, separators=(',', ':'), ensure_ascii=False))


def ok(data):
    return {'success': True, 'message': 'fixture', 'error_code': 0, 'error_name': 'swSuccess', 'data': data}


class ReferenceAnswers(unittest.TestCase):
    """Answer sizes on the 88-node reference program (changes.md, P0-2)."""

    @classmethod
    def setUpClass(cls):
        cls.program = run_program(FIXTURE.read_text(encoding='utf-8'))
        nodes = cls.program['nodes']
        cls.components = {}
        for node in nodes:
            live = node['id'] in BODIES
            cls.components[node['id']] = {
                'feature': node['id'], 'bodies': [node['id']] if live else [], 'face_count': 40 if live else 0,
                'kind': node['kind'], **({} if live else {'consumed_by': 'TorsoSolid'}),
                **({'dependencies': node['geometry']['seeds']} if 'seeds' in node['geometry'] else {})}
        cls.body_state = {'count': 9, 'bbox_unit': 'mm', 'bodies': [
            {'name': name, 'visible': True, 'face_count': 61,
             'bbox': {'min': [-20.635912345, 46.4966, -16.007], 'max': [20.6358, 100.8163, 17.432]}}
            for name in BODIES]}
        cls.context = {
            'mcp_version': '6.25.0', 'shape_program_version': 3,
            'coordinate_system': {'unit': 'mm', 'up': 'Y', 'width': 'X', 'depth': 'Z'},
            'native_parameter_bindings': True, 'in_place_edit': True, 'workflow': 'reconstruct_shape',
            'document': {'title': 'Bender.SLDPRT', 'path': 'D:\\TMP\\bender\\work\\Bender.SLDPRT', 'dirty': False, 'type': 1},
            'entry_points': ['execute_model_code'] * 7, 'constructors': ['parameter'] * 18,
            'defaults': {'observations': 'automatic after create/edit'},
            'body_state': cls.body_state, 'document_editable': True,
            'document_editable_meaning': 'embedded_program_available',
            'native_rebuild_state': {'status': 'READY', 'pending': []},
            'revision': 'a' * 64, 'parameters': {'H': 150, 'Clr': 0.25}, 'components': cls.components,
            'model_id': 'b' * 32, 'document_edit_scope': 'dependency_reconcile',
            'edit_compatibility': {'status': 'NOT_CHECKED', 'inventory_checked': False,
                                   'definition_edit': 'CONDITIONAL', 'note': 'x' * 80}}

    def test_reference_program_shape(self):
        self.assertEqual(len(self.program['nodes']), 88)
        self.assertGreater(size(self.context), 9000)

    def test_context_summary_is_small_and_sufficient(self):
        summary = compact_context(self.context)
        self.assertLessEqual(size(ok(summary)), 3000)
        for key in ('mcp_version', 'document', 'revision', 'parameters', 'bodies', 'component_count',
                    'native_rebuild_state', 'edit_compatibility'):
            self.assertIn(key, summary)
        self.assertEqual(summary['component_count'], 88)
        self.assertEqual(summary['edit_compatibility'], {'status': 'NOT_CHECKED'})
        self.assertEqual([body['name'] for body in summary['bodies']], BODIES)
        self.assertEqual(set(summary['bodies'][0]), {'name', 'face_count', 'bbox'})
        self.assertEqual(summary['bodies'][0]['bbox']['min'][0], -20.6359)
        for static in ('components', 'entry_points', 'constructors', 'defaults', 'coordinate_system'):
            self.assertNotIn(static, summary)

    def test_context_components_and_full_detail(self):
        result = ok(self.context)
        picked = compact_response('get_model_context', {'components': ['Head', 'Missing']}, result)['data']
        self.assertEqual(list(picked['components']), ['Head'])
        self.assertEqual(picked['unknown_components'], ['Missing'])
        full = compact_response('get_model_context', {'detail': 'full'}, result)
        self.assertIs(full, result)
        self.assertEqual(len(full['data']['components']), 88)

    def test_reconcile_answer_is_the_summary_plus_reconciliation(self):
        compact = compact_response('get_model_context', {}, ok(self.context))
        compact['data']['reconciliation'] = {'state_inspected': True, 'original_operation_replayed': False,
                                             'solidworks_restart_verified': False,
                                             'acceptance': 'CURRENT_STATE_ONLY; inspect the model before further edits'}
        self.assertLessEqual(size(compact), 3500)

    def edit_result(self):
        names = list(self.components)
        images = [{'view': view, 'path': f'D:\\TMP\\bender\\work\\Bender_views\\{"c" * 32}\\{view}.png',
                   'document': 'D:\\TMP\\bender\\work\\Bender.SLDPRT', 'revision': 'a' * 64,
                   'configuration': 'Default', 'size_px': [1000, 1000], 'sha256': 'd' * 64,
                   'native_scale': 1.5, 'perspective': False} for view in ('front', 'right', 'isometric')]
        return {'components': self.components, 'status': 'NATIVE_GEOMETRY_UPDATED',
                'native_parameter_bindings': True, 'in_place_edit': True, 'compiler_version': 3,
                'document': 'D:\\TMP\\bender\\work\\Bender.SLDPRT', 'program_hash': 'e' * 64,
                'updated_curve_count': 12, 'native_feature_ids_preserved': False,
                'preserved_components': names[:-3], 'rebuilt_components': names[-3:],
                'removed_components': ['OldMask'], 'added_components': [names[-3]], 'added_parameters': [],
                'obsolete_features_removed': ['HeadShell', 'HeadCarved', 'Head'],
                'edit_strategy': 'dependency_reconcile', 'committed_document': 'D:\\TMP\\bender\\work\\Bender.SLDPRT',
                'native_file_preserved': True, 'native_mutation_started': True, 'revision': 'f' * 64,
                'parameters': {'H': 150.0, 'Clr': 0.25}, 'body_state': self.body_state,
                'working_view': {'verified': True, 'status': 'FRAMED', 'bodies': BODIES, 'scale': 1.25,
                                 'bbox_mm': {'min': [0, 0, 0], 'max': [1, 1, 1]}},
                'timings_ms': {'preflight': 900, 'checkpoint': 40, 'native_update': 50000, 'save': 2000},
                'images': images, 'warnings': [], 'acceptance': 'EXECUTED_NOT_DELIVERY_VALIDATED'}

    def test_edit_answer_lists_what_changed(self):
        full = self.edit_result()
        self.assertGreater(size(full), 9000)
        answer = compact_mutation(full)
        self.assertLessEqual(size(ok(answer)), 3000)
        for key in ('revision', 'parameters', 'bodies', 'rebuilt_components', 'added_components',
                    'removed_components', 'preserved_count', 'warnings', 'timings_ms', 'images'):
            self.assertIn(key, answer)
        self.assertEqual(answer['preserved_count'], 85)
        self.assertEqual(answer['working_view'], {'verified': True})
        self.assertEqual(set(answer['images'][0]), {'view', 'path'})
        # Component records only for what was rebuilt or added.
        self.assertEqual(list(answer['components']), full['rebuilt_components'])
        self.assertNotIn('preserved_components', answer)

    def test_failed_working_view_keeps_its_details(self):
        full = self.edit_result()
        full['working_view'] = {'verified': False, 'status': 'ZOOM_FAILED', 'error': 'no view'}
        self.assertEqual(compact_mutation(full)['working_view']['status'], 'ZOOM_FAILED')

    def test_create_answer_does_not_repeat_the_component_map(self):
        full = {**self.edit_result(), 'status': 'NATIVE_GEOMETRY_CREATED',
                'program_parameters': self.program['parameters']}
        for key in ('parameters', 'rebuilt_components', 'added_components', 'preserved_components'):
            full.pop(key)
        answer = compact_mutation(full)
        self.assertEqual(answer['parameters'], {'H': 150.0, 'Clr': 0.25})
        self.assertEqual(len(answer['added_components']), 88)
        self.assertNotIn('components', answer)
        self.assertLessEqual(size(ok(answer)), 4000)

    def test_analysis_summary(self):
        full = {'status': 'PROGRAM_VALIDATED', **self.program}
        self.assertGreater(size(full), 20000)
        answer = compact_response('execute_model_code', {}, ok(full))
        self.assertLessEqual(size(answer), 2000)
        data = answer['data']
        self.assertEqual(data['nodes_by_kind']['tube'], 30)
        self.assertEqual(data['node_count'], 88)
        self.assertEqual(data['parameters'], {'H': 150.0, 'Clr': 0.25})
        self.assertIn('Head', data['components'])
        self.assertIn('stdout', data)
        self.assertNotIn('source_graph', data)

    def test_inspection_states_methods_once(self):
        pair = {'relation': 'SEPARATED', 'boundary_distance_mm': 0.25000000000001421,
                'clearance_mm': 0.25000000000001421, 'intersection_volume_mm3': 0.0, 'interference': False,
                'errors': [], 'volume_method': 'IBody2.GetMassProperties_numerical_integration',
                'contact_threshold_mm': 1e-05,
                'coverage': 'Global body-pair minimum; not uniform local mating clearance, wall thickness or print readiness',
                'closest_points_mm': [[1.123456789012, 2.0, 3.0], [1.373456789012, 2.0, 3.0]],
                'distance_method': 'IModelDoc2.ClosestDistance_solid_bodies',
                'intersection_method': 'IBody2.Operations2_intersection_of_temporary_copies',
                'clearance_check': {'min_clearance_mm': 0.2, 'max_clearance_mm': 0.3, 'passed': True,
                                    'numeric_slack_mm': 1e-06}}
        facts = [{'name': name, 'face_count': 61, 'volume_mm3': 12345.678901234567,
                  'bbox': {'min': [-20.635912345678, 0, 0], 'max': [1, 2, 3]},
                  'bbox_method': 'IBody2.GetExtremePoint', 'dimensions_mm': [21.635912345678, 2, 3],
                  'kernel_health': {'method': 'IBody2.Check3', 'fault_count': 0, 'passed': True},
                  'surface_types': {'plane': 4, 'cylinder': 20}} for name in BODIES]
        full = {'document_path': 'D:\\x.SLDPRT', 'revision': 'a' * 64, 'unit': 'mm', 'bodies': facts,
                'bbox': {'min': [0, 0, 0], 'max': [1, 2, 3]}, 'parameters': {'H': 150.0},
                'sections': [], 'cad_entities_created': 0,
                'body_pairs': [{'id': f'fit{i}', 'a': {'body': 'Head'}, 'b': {'body': 'Torso'},
                                'body_a': 'Head', 'body_b': 'Torso', **pair} for i in range(8)]}
        answer = compact_inspection(full)
        self.assertLessEqual(size(ok(answer)), 10000)
        self.assertLess(size(answer), size(full) * .75)
        self.assertIn('coverage', answer['body_pair_methods'])
        self.assertEqual(answer['body_pair_methods']['numeric_slack_mm'], 1e-06)
        first = answer['body_pairs'][0]
        for repeated in ('volume_method', 'distance_method', 'intersection_method', 'coverage', 'errors'):
            self.assertNotIn(repeated, first)
        self.assertEqual(first['boundary_distance_mm'], 0.25)
        self.assertIs(first['clearance_check']['passed'], True)
        self.assertEqual(answer['bodies'][0]['bbox']['min'][0], -20.635912)

    def test_pair_errors_and_differing_methods_stay_with_the_pair(self):
        full = {'body_pairs': [
            {'id': 'a', 'relation': 'UNKNOWN', 'errors': [{'phase': 'distance', 'code': 'X'}],
             'distance_method': 'one', 'clearance_check': {'passed': None, 'numeric_slack_mm': 1e-6}},
            {'id': 'b', 'relation': 'SEPARATED', 'errors': [], 'distance_method': 'two'}]}
        answer = compact_inspection(full)
        self.assertEqual(answer['body_pairs'][0]['errors'][0]['code'], 'X')
        self.assertEqual(answer['body_pairs'][0]['distance_method'], 'one')
        self.assertIsNone(answer['body_pairs'][0]['clearance_check']['passed'])
        self.assertNotIn('distance_method', answer['body_pair_methods'])

    def test_rounding_keeps_types(self):
        self.assertEqual(rounded({'a': [1.00000004, True, None, 'x', 2]}), {'a': [1, True, None, 'x', 2]})


class BoundaryTests(unittest.TestCase):
    def test_dispatch_is_compact_unless_verbose(self):
        code = FIXTURE.read_text(encoding='utf-8')
        compact = dispatch_new_tool(server.sw_automation, 'execute_model_code', {'code': code})
        self.assertTrue(compact['success'])
        self.assertNotIn('nodes', compact['data'])
        # 1.6k for the summary itself plus about 0.6k of predicted extents (global-changes.md, G1).
        self.assertLessEqual(size(compact), 2850)   # Includes warnings and three numerical budget fields.
        verbose = dispatch_new_tool(server.sw_automation, 'execute_model_code', {'code': code, 'verbose': True})
        self.assertEqual(len(verbose['data']['nodes']), 88)
        self.assertGreater(size(verbose), 20000)

    def test_detail_is_validated(self):
        result = dispatch_new_tool(server.sw_automation, 'get_model_context', {'detail': 'everything'})
        self.assertFalse(result['success'])

    def test_validation_errors_carry_no_traceback(self):
        bad = "model.rounded_prism('A', 10, 10, 5, 9)"
        result = dispatch_new_tool(server.sw_automation, 'execute_model_code', {'code': bad})
        self.assertFalse(result['success'])
        self.assertEqual(result['data']['error']['code'], 'INVALID_ROUNDED_PRISM')
        self.assertNotIn('traceback', result['data']['error']['details'])
        self.assertNotIn('Traceback', json.dumps(result))
        with patch.dict(os.environ, {'SOLIDWORKS_MCP_DEBUG': '1'}):
            debug = dispatch_new_tool(server.sw_automation, 'execute_model_code', {'code': bad})
        self.assertIn('traceback', debug['data']['error']['details'])
        unexpected = dispatch_new_tool(server.sw_automation, 'execute_model_code', {'code': 'model.dome(1)'})
        self.assertIn('traceback', unexpected['data']['error']['details'])

    def test_executor_status_carries_a_summary_not_the_result(self):
        with tempfile.TemporaryDirectory() as folder:
            executor = StaExecutor(Path(folder) / 'session.lock', identity_provider=lambda: [],
                                   initialize=lambda: None, finalize=lambda: None, pump=lambda: None)
            try:
                big = ok({'components': {f'C{i}': {'feature': f'C{i}', 'bodies': []} for i in range(300)},
                          'revision': 'a' * 64, 'status': 'NATIVE_GEOMETRY_UPDATED'})
                big['message'] = 'm' * 5000
                op = executor.submit('execute_model_code', {'document_path': 'D:\\x.SLDPRT'}, lambda: big)
                self.assertTrue(op.response.result(2)['success'])
                while executor.status()['worker_busy']:
                    pass
                status = executor.status(op.id)
                self.assertLessEqual(size(ok(status)), 1500)
                self.assertNotIn('result', status['operation'])
                self.assertEqual(status['operation']['result_summary']['revision'], 'a' * 64)
                self.assertIn('components', executor.status(op.id, include_result=True)['operation']['result']['data'])
            finally:
                executor.close()


if __name__ == '__main__':
    unittest.main()
