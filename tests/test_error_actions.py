"""Frequent errors name a real tool and its arguments (changes.md, P2-8)."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solidworks_mcp import server
from solidworks_mcp.automation.runtime import atomic_json_write
from solidworks_mcp.automation.sta_executor import StaExecutor, failure
from solidworks_mcp.error_actions import attach_recovery, recovery_actions
from solidworks_mcp.shape_program import ProgramError, run_program

CODES = {
    'MODAL_DIALOG_BLOCKING': {},
    'SESSION_QUARANTINED': {'operation_id': 'op1', 'cause': 'MCP_OWNER_PROCESS_EXITED'},
    'DOCUMENT_MISMATCH': {},
    'STALE_REVISION': {},
    'OUTPUT_EXISTS': {},
    'COMPONENT_CONSUMED': {'component': 'HeadCylinder', 'consumed_by': 'HeadSolid'},
    'UNSUPPORTED_SURFACE_HOST': {},
    'INVALID_SURFACE_SCOPE': {},
    'NATIVE_RECIPE_FAILED': {'stage': 'native_surface_patch'},
    'NATIVE_REBUILD_FAILED': {'component': 'LegTubeL', 'features': [{'feature': 'LegTubeL', 'component': 'LegTubeL'}]},
    'NATIVE_BODY_INVALID': {'faults': {'body': 'Torso', 'components': ['ShoulderCone']}},
    'NATIVE_PATH_FAILED': {'feature': 'Leg_Path5', 'component': 'Leg'},
    'IDEMPOTENCY_STATE_CHANGED': {'key': 'auto:abc'},
    'NATIVE_REBUILD_STALE': {'sketches': ['Leg_Path1'], 'rebuilds': 2},
}
ARGUMENTS = {'document_path': 'D:\\work\\part.SLDPRT', 'expected_revision': 'r1', 'directory': 'D:\\work\\out'}


class RecoveryActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        catalog = server._all_tools() + [server._expert_tool()]
        cls.tools = {tool.name: tool for tool in catalog}
        cls.public = {tool.name for tool in asyncio.run(server.list_tools())}

    def assert_executable(self, item):
        """The action must be callable exactly as written."""
        self.assertIn(item['tool'], self.public, item)
        self.assertTrue(item['why'])
        if item['tool'] == 'expert_tools':
            nested = item['arguments']
            self.assertEqual(nested['action'], 'call')
            self.assertIn(nested['name'], self.tools, item)
            self.assertNotIn(nested['name'], self.public, 'Public tools are called directly')
            schema = self.tools[nested['name']].inputSchema
            arguments = nested['arguments']
        else:
            schema = self.tools[item['tool']].inputSchema
            arguments = item['arguments']
        self.assertLessEqual(set(arguments), set(schema.get('properties', {})), item)

    def test_every_listed_error_names_a_registered_tool(self):
        for code, details in CODES.items():
            for tool in ('execute_model_code', 'export_model', 'observe_model'):
                with self.subTest(code=code, tool=tool):
                    actions = recovery_actions(code, tool, ARGUMENTS, details)
                    self.assertTrue(actions, code)
                    for item in actions:
                        self.assert_executable(item)

    def test_actions_are_specific(self):
        stale = recovery_actions('STALE_REVISION')
        self.assertEqual(stale[0]['tool'], 'get_model_context')
        export = recovery_actions('OUTPUT_EXISTS', 'export_model', ARGUMENTS)[0]
        self.assertEqual(export['tool'], 'export_model')
        self.assertIs(export['arguments']['replace'], True)
        self.assertEqual(export['arguments']['directory'], ARGUMENTS['directory'])
        self.assertEqual(recovery_actions('OUTPUT_EXISTS', 'execute_model_code')[0]['tool'], 'execute_model_code')
        modal = recovery_actions('MODAL_DIALOG_BLOCKING')
        self.assertEqual([a['arguments']['name'] for a in modal], ['get_environment_status', 'recover_environment'])
        opened = recovery_actions('DOCUMENT_MISMATCH', 'inspect_model', ARGUMENTS)
        self.assertEqual(opened[1]['arguments']['arguments'], {'filepath': ARGUMENTS['document_path']})
        consumed = recovery_actions('COMPONENT_CONSUMED', details=CODES['COMPONENT_CONSUMED'])[0]
        self.assertEqual(consumed['arguments']['components'], ['HeadCylinder', 'HeadSolid'])
        self.assertIn('HeadSolid', consumed['why'])
        self.assertEqual(recovery_actions('UNSUPPORTED_SURFACE_HOST')[0]['arguments'], {'topic': 'surface'})
        busy = recovery_actions('CAD_SESSION_BUSY', details={'operation_id': 'op7'})[0]
        self.assertEqual((busy['tool'], busy['arguments']), ('wait_operation', {'operation_id': 'op7'}))
        self.assertEqual(recovery_actions('SOMETHING_ELSE'), [])

    def test_a_path_failure_can_be_repeated_as_written(self):
        create = recovery_actions('NATIVE_PATH_FAILED', 'execute_model_code',
                                  {'mode': 'create', 'output_path': 'D:\\work\\new.SLDPRT', 'code': 'x'},
                                  CODES['NATIVE_PATH_FAILED'])[0]
        # A failed create that left no file runs again under the same request key.
        self.assertEqual(create['arguments'], {'mode': 'create', 'output_path': 'D:\\work\\new.SLDPRT'})
        self.assertIn('Leg', create['why'])
        # A failed edit is replayed from its receipt unless the key is new.
        edit = recovery_actions('NATIVE_PATH_FAILED', 'execute_model_code', {'mode': 'edit', **ARGUMENTS})[0]
        self.assertTrue(edit['arguments']['idempotency_key'].startswith('retry-'))
        self.assertEqual(edit['arguments']['document_path'], ARGUMENTS['document_path'])

    def test_a_rebuild_failure_does_not_assume_its_cause(self):
        actions = recovery_actions('NATIVE_REBUILD_FAILED', 'execute_model_code', ARGUMENTS,
                                   {'component': 'Rim', 'features': [{'feature': 'Rim', 'type': 'Fillet'}]})
        self.assertEqual(actions[0]['arguments'], {'components': ['Rim']})
        self.assertNotIn('tube', actions[1]['why'])

    def test_quarantine_explains_restart_only_for_a_dead_owner(self):
        dead = recovery_actions('SESSION_QUARANTINED', details=CODES['SESSION_QUARANTINED'])
        self.assertEqual([a['tool'] for a in dead], ['get_executor_status', 'expert_tools', 'reconcile_session'])
        self.assertEqual(dead[1]['arguments']['name'], 'restart_solidworks')
        self.assertEqual(dead[2]['arguments'], {'operation_id': 'op1'})
        late = recovery_actions('SESSION_QUARANTINED', details={'operation_id': 'op2'})
        self.assertEqual([a['tool'] for a in late], ['get_executor_status', 'reconcile_session'])

    def test_attach_handles_both_result_shapes_and_leaves_success_alone(self):
        flat = attach_recovery('observe_model', ARGUMENTS, failure('STALE_REVISION', 'changed'))
        self.assertEqual(flat['data']['recommended_actions'][0]['tool'], 'get_model_context')
        structured = server.sw_automation._error('COMPONENT_CONSUMED', 'x', details={'component': 'A', 'consumed_by': 'C'},
                                                 recommended_actions=['Free text'])
        attach_recovery('inspect_model', ARGUMENTS, structured)
        actions = structured['data']['error']['recommended_actions']
        self.assertEqual(actions[0]['arguments']['components'], ['A', 'C'])
        self.assertTrue(all(isinstance(item, dict) for item in actions))
        success = {'success': True, 'data': {}}
        self.assertEqual(attach_recovery('observe_model', {}, success), {'success': True, 'data': {}})

    def test_program_reports_who_consumed_a_component(self):
        code = ('a=model.dome("A",4,2)\nb=model.dome("B",4,2)\nc=model.combine("C",[a,b])\n'
                'model.mirror("M",[a])\n')
        with self.assertRaises(ProgramError) as raised:
            run_program(code)
        self.assertEqual(raised.exception.code, 'COMPONENT_CONSUMED')
        self.assertEqual(raised.exception.details, {'component': 'A', 'consumed_by': 'C'})

    def test_server_answers_carry_the_actions(self):
        with tempfile.TemporaryDirectory() as folder:
            executor = StaExecutor(Path(folder) / 'session.lock', identity_provider=lambda: [],
                                   initialize=lambda: None, finalize=lambda: None, pump=lambda: None)
            try:
                stale = failure('STALE_REVISION', 'The requested starting revision changed')
                with patch.object(server, 'cad_executor', executor), \
                     patch.object(server, '_call_tool_direct', return_value=stale):
                    answer = asyncio.run(server.call_tool('get_document_info', {}))
                self.assertTrue(answer.isError)
                self.assertEqual(answer.structuredContent['data']['recommended_actions'][0]['tool'], 'get_model_context')
                # An orphaned journal: the quarantine error says why and what to run.
                atomic_json_write(executor.state_path, {'status': 'RUNNING', 'operation_id': 'orphan',
                                                        'owner_pid': 999999, 'owner_started_at': 1.0})
                with patch.object(server, 'cad_executor', executor):
                    blocked = asyncio.run(server.call_tool('get_document_info', {}))
                data = blocked.structuredContent['data']
                self.assertEqual(blocked.structuredContent['error_name'], 'SESSION_QUARANTINED')
                self.assertEqual(data['cause'], 'MCP_OWNER_PROCESS_EXITED')
                self.assertIs(data['owner_alive'], False)
                self.assertEqual(data['unresolved_operation_id'], 'orphan')
                self.assertEqual(data['recovery']['tool'], 'restart_solidworks')
                actions = data['recommended_actions']
                self.assertEqual([(a['tool'], a['arguments'].get('name')) for a in actions],
                                 [('get_executor_status', None), ('expert_tools', 'restart_solidworks'),
                                  ('reconcile_session', None)])
                self.assertEqual(actions[2]['arguments'], {'operation_id': 'orphan'})
            finally:
                executor.close()


if __name__ == '__main__':
    unittest.main()
