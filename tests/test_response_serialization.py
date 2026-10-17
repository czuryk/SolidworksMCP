"""Response failures stay local to a call; all fixtures are COM-free."""
import asyncio
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import JSONRPCMessage, JSONRPCResponse

from solidworks_mcp import server
from solidworks_mcp.automation.request_execution import execute_request
from solidworks_mcp.automation.request_journal import RequestJournal
from solidworks_mcp.session_trace import SessionTrace, session_report


def bad_result(operation_id='fixture'):
    return {'success': True, 'message': 'Handler completed', 'error_code': 0,
            'error_name': 'swSuccess', 'data': {'executor': {'operation_id': operation_id},
            'levels': {'passed': np.int64(24)}, 'timings_ms': {'total': 1200}}}


def wire_result(rendered):
    envelope = JSONRPCMessage(JSONRPCResponse(jsonrpc='2.0', id=7,
        result=rendered.model_dump(by_alias=True)))
    return json.loads(envelope.model_dump_json(by_alias=True, exclude_none=True))['result']


class ResponseSerializationTests(unittest.TestCase):
    def test_unsupported_number_is_an_error_without_changing_handler_result(self):
        original = bad_result()
        original['data']['request_receipt'] = {'key': 'saved', 'status': 'COMPLETED'}
        rendered = server._executor_content('compare_reference', original)
        result = wire_result(rendered)['structuredContent']
        self.assertTrue(rendered.isError)
        self.assertEqual(result['error_name'], 'RESULT_SERIALIZATION_ERROR')
        self.assertEqual(result['data']['operation_id'], 'fixture')
        self.assertEqual(result['data']['request_receipt'], original['data']['request_receipt'])
        self.assertTrue(result['data']['response_preparation']['handler_success'])
        self.assertIs(type(original['data']['levels']['passed']), np.int64)
        trace = SessionTrace()
        entry = trace.record('compare_reference', {}, result, .1, 100, 0)
        self.assertFalse(entry['success'])
        self.assertTrue(entry['handler_success'])
        self.assertFalse(entry['serialization_success'])

    def test_failed_handler_and_circular_payload_also_survive(self):
        original = {'success': False, 'error_name': 'NATIVE_RECIPE_FAILED', 'data': {}}
        original['data']['cycle'] = original
        rendered = server._executor_content('execute_model_code', original)
        result = wire_result(rendered)['structuredContent']
        trace = SessionTrace()
        trace.record('execute_model_code', {'mode': 'create'}, result, .1, 100, 0)
        report = session_report(trace.snapshot())
        self.assertEqual(report['mutations_failed'], 1)
        self.assertEqual(report['operation_errors_by_code'], {'NATIVE_RECIPE_FAILED': 1})
        self.assertEqual(report['response_serialization_failures'], 1)

    def test_bad_metadata_cannot_break_the_fallback(self):
        original = bad_result()
        original['data']['executor']['operation_id'] = np.array([1, 2])
        original['data']['operation_id'] = 'safe-id'
        original['data']['request_receipt'] = {'bad': object()}
        result = wire_result(server._executor_content('compare_reference', original))['structuredContent']
        self.assertEqual(result['data']['operation_id'], 'safe-id')
        self.assertEqual(result['error_name'], 'RESULT_SERIALIZATION_ERROR')

    def test_rendering_failure_preserves_committed_receipt_and_does_not_repeat_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'part.SLDPRT'
            path.write_bytes(b'saved fixture')
            journal = RequestJournal(Path(folder) / 'requests.sqlite3')
            args = {'mode': 'edit', 'document_path': str(path), 'expected_revision': 'before',
                    'parameters': {'H': 44}, 'idempotency_key': 'once'}
            state = [('before', {'file': 'before'})]

            def mutate():
                state[0] = ('after', {'file': 'after'})
                return {'success': True, 'message': 'Saved', 'data': {'revision': 'after'}}

            callback = Mock(side_effect=mutate)
            with patch('solidworks_mcp.automation.request_execution.native_state', side_effect=lambda *_: state[0]):
                original = execute_request(None, 'execute_model_code', args, callback, journal)
                receipt = copy.deepcopy(journal.get('once'))
                original['data']['bad'] = np.int64(24)
                for _ in range(2):
                    rendered = server._executor_content('execute_model_code', original)
                    self.assertEqual(wire_result(rendered)['structuredContent']['data']['request_receipt']['status'], 'COMPLETED')
                self.assertEqual(journal.get('once'), receipt)
                replay = execute_request(None, 'execute_model_code', args, callback, journal)
            self.assertTrue(replay['data']['request_receipt']['replayed'])
            callback.assert_called_once()


class StdioResponseIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_and_wait_errors_keep_the_same_stdio_process_alive(self):
        async def exercise():
            params = StdioServerParameters(command=sys.executable,
                args=[str(Path(__file__).resolve()), '--stdio-fixture'])
            with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as errors:
                async with stdio_client(params, errlog=errors) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        before = (await client.call_tool('get_executor_status', {})).structuredContent
                        direct = await client.call_tool('model_api_lookup', {'topic': 'direct'})
                        self.assertEqual(direct.structuredContent['error_name'], 'RESULT_SERIALIZATION_ERROR')
                        running = await client.call_tool('model_api_lookup', {'topic': 'deferred'})
                        self.assertEqual(running.structuredContent['data']['status'], 'RUNNING')
                        waited = await client.call_tool('wait_operation', {'operation_id': 'deferred'})
                        self.assertEqual(waited.structuredContent['error_name'], 'RESULT_SERIALIZATION_ERROR')
                        after = (await client.call_tool('get_executor_status', {})).structuredContent
                        self.assertTrue(after['success'])
                        self.assertEqual(before['data']['server']['pid'], after['data']['server']['pid'])
                        report = await client.call_tool('expert_tools', {'action': 'call', 'name': 'get_session_report'})
                        self.assertEqual(report.structuredContent['data']['response_serialization_failures'], 2)
        await asyncio.wait_for(exercise(), timeout=25)


def run_stdio_fixture():
    # Only the backend is substituted. Dispatch, tracing, MCP framing and the
    # actual SDK stdout writer remain the production implementations.
    original_dispatch = server.dispatch_new_tool

    def dispatch(automation, name, arguments):
        if name == 'model_api_lookup':
            if arguments['topic'] == 'deferred':
                return {'success': True, 'message': 'Running fixture', 'data': {
                    'status': 'RUNNING', 'operation_id': 'deferred'}}
            return bad_result('direct')
        return original_dispatch(automation, name, arguments)

    async def wait(operation_id, wait_sec):
        return 'model_api_lookup', bad_result(operation_id)

    server.dispatch_new_tool = dispatch
    server._wait_operation = wait
    server.cad_executor = SimpleNamespace(
        find=lambda operation_id: SimpleNamespace(name='model_api_lookup', arguments={}),
        status=lambda *args, **kwargs: {'busy': False})
    server.session_trace = SessionTrace()
    server.solidworks_memory = lambda: {}
    asyncio.run(server.main())


if __name__ == '__main__':
    if '--stdio-fixture' in sys.argv:
        run_stdio_fixture()
    else:
        unittest.main()
