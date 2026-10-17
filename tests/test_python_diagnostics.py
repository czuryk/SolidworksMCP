"""Raw-code failures retain evidence and do not trigger a second native call."""
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from solidworks_mcp import server
from solidworks_mcp.automation.python_diagnostics import execution_failure
from solidworks_mcp.automation.runtime import enrich_legacy_error
from solidworks_mcp.shape_program import ProgramError


class PythonDiagnosticsTests(unittest.TestCase):
    def run_code(self, code, *, extra=None, guard=None):
        automation = SimpleNamespace(is_connected=True, app=SimpleNamespace(ActiveDoc=None))
        context = {'result': None, 'com_get': Mock(), **(extra or {})}
        before = sys.stdout, sys.stderr
        with patch.object(server, 'sw_automation', automation), \
             patch.object(server, '_build_exec_context', return_value=context), \
             patch.object(server, 'cad_executor', Mock()), \
             patch.object(server, 'StaWatchdog', return_value=guard):
            result = server._execute_python_fixed(code, {'enabled': True} if guard else None)
        self.assertEqual((sys.stdout, sys.stderr), before)
        return result

    def test_partial_output_and_traceback_survive_runtime_failure(self):
        effects = []
        result = self.run_code("import sys\nprint('before native')\n"
            "print('native warning', file=sys.stderr)\neffects.append(1)\n"
            "raise AssertionError('body missing')", extra={'effects': effects})
        self.assertEqual(effects, [1])
        self.assertFalse(result['success'])
        data = result['data']
        self.assertEqual(data['stdout'], 'before native\n')
        self.assertEqual(data['stderr'], 'native warning\n')
        self.assertIn('<mcp-execute-python>', data['traceback'])
        self.assertIn('body missing', data['traceback'])
        self.assertIn('before native', result['message'])
        self.assertIn('native warning', result['message'])
        error = data['error']
        self.assertEqual(error['code'], 'PYTHON_EXECUTION_ERROR')
        self.assertEqual(error['details']['script_state'], 'PARTIAL_EFFECTS_POSSIBLE')
        self.assertFalse(error['details']['automatic_retry'])
        self.assertIsNone(error['document_restored'])
        self.assertFalse(error['recoverable'])
        self.assertIs(enrich_legacy_error(result), result)
        self.assertEqual(error['code'], 'PYTHON_EXECUTION_ERROR')

    def test_syntax_error_never_connects_or_starts_watchdog(self):
        with patch.object(server, '_build_exec_context') as context, \
             patch.object(server.sw_automation, 'connect') as connect, \
             patch.object(server, 'StaWatchdog') as guard:
            result = server._execute_python_fixed('print(\n', {'enabled': True})
        context.assert_not_called()
        connect.assert_not_called()
        guard.assert_not_called()
        self.assertEqual(result['data']['error']['code'], 'PYTHON_SYNTAX_ERROR')
        self.assertEqual(result['data']['error']['details']['script_state'], 'NOT_EXECUTED')
        self.assertEqual(result['data']['stdout'], '')

    def test_success_contract_and_stream_restoration_unchanged(self):
        result = self.run_code("print('ok')\nresult={'value': 3}")
        self.assertTrue(result['success'])
        self.assertEqual(result['data']['stdout'], 'ok\n')
        self.assertEqual(result['data']['result'], "{'value': 3}")
        self.assertNotIn('error', result['data'])

    def test_native_error_code_and_details_retained(self):
        result = self.run_code("raise error", extra={'error': ProgramError(
            'FEATURE_DEAD', 'No solid', feature='Test')})
        error = result['data']['error']
        self.assertEqual(error['code'], 'FEATURE_DEAD')
        self.assertEqual(error['details']['native_details'], {'feature': 'Test'})

    def test_hresult_does_not_misclassify_disconnected_object_as_member_error(self):
        for hr, code in [(0x80010108, 'COM_OBJECT_DISCONNECTED'),
                         (0x800401FD, 'COM_OBJECT_DISCONNECTED'),
                         (0x80020003, 'COM_MEMBER_MISMATCH'),
                         (0x80020006, 'COM_MEMBER_MISMATCH'),
                         (0x80004005, 'COM_CALL_FAILED')]:
            for value in (hr, hr - 2**32):
                with self.subTest(hresult=value):
                    exc = RuntimeError('Native error')
                    exc.hresult = value
                    data = execution_failure(exc, 'execute_python', 'trace', 'output', '')['data']
                    self.assertEqual(data['error']['code'], code)
                    self.assertEqual(data['error']['com_hresult'], value)

    def test_preflight_exception_reports_unexecuted_script(self):
        with patch.object(server, 'sw_automation', SimpleNamespace(is_connected=True, app=None)), \
             patch.object(server, '_build_exec_context', side_effect=RuntimeError('Context failed')):
            result = server._execute_python_fixed('print(1)')
        self.assertEqual(result['data']['error']['stage'], 'preflight')
        self.assertFalse(result['data']['error']['details']['script_execution_started'])

    def test_script_exit_and_interrupt_retain_evidence(self):
        for exception in [SystemExit(2), KeyboardInterrupt()]:
            result = self.run_code("print('before interruption')\nraise error",
                                   extra={'error': exception})
            self.assertEqual(result['data']['error']['code'], 'PYTHON_SCRIPT_INTERRUPTED')
            self.assertEqual(result['data']['stdout'], 'before interruption\n')
            self.assertEqual(result['data']['error']['details']['script_state'], 'PARTIAL_EFFECTS_POSSIBLE')

    def test_noninteger_hresult_cannot_hide_the_original_python_failure(self):
        exc = ValueError('original error')
        exc.hresult = 'not an HRESULT'
        result = self.run_code("print('retained')\nraise error", extra={'error': exc})
        self.assertEqual(result['data']['error']['code'], 'PYTHON_EXECUTION_ERROR')
        self.assertEqual(result['data']['stdout'], 'retained\n')

    def test_watchdog_cleanup_once_and_original_failure_preserved(self):
        guard = Mock()
        guard.close.side_effect = RuntimeError('Cleanup failed')
        result = self.run_code("print('evidence')\nraise ValueError('Primary failure')", guard=guard)
        guard.close.assert_called_once()
        self.assertEqual(result['data']['error_message'], 'Primary failure')
        self.assertEqual(result['data']['stdout'], 'evidence\n')
        self.assertIn('Cleanup failed', result['data']['watchdog']['cleanup_error'])

    def test_successful_execution_with_failed_cleanup_is_not_reported_as_success(self):
        guard = Mock()
        guard.close.side_effect = RuntimeError('Cleanup failed')
        result = self.run_code("print('finished')", guard=guard)
        guard.close.assert_called_once()
        self.assertFalse(result['success'])
        self.assertEqual(result['data']['error']['code'], 'WATCHDOG_CLEANUP_FAILED')
        self.assertEqual(result['data']['stdout'], 'finished\n')

    def test_watchdog_start_failure_does_not_execute_and_closes_once(self):
        effects = []
        guard = Mock()
        guard.start.side_effect = RuntimeError('Not ready')
        guard.close.return_value = {'closed': True}
        result = self.run_code('effects.append(1)', extra={'effects': effects}, guard=guard)
        guard.close.assert_called_once()
        self.assertEqual(effects, [])
        self.assertFalse(result['success'])

    def test_dispatch_does_not_probe_com_after_raw_failure(self):
        failure = execution_failure(ProgramError('NATIVE_WORKER_LOST',
            'Partial operation', native_outcome_unknown=True),
            'execute_python', 'trace', 'retained\n', '')
        with patch.object(server, '_execute_python_fixed', return_value=failure), \
             patch.object(server, 'SessionLease', Mock()), \
             patch.object(server.sw_automation, 'ui_preflight', return_value=None), \
             patch.object(server.sw_automation, 'ui_postflight',
                          side_effect=AssertionError('Postflight after failure')), \
             patch.object(server.sw_automation, '_document_snapshot',
                          side_effect=[{}, AssertionError('COM after failure')]) as snapshot:
            result = server._call_tool_direct('execute_python', {'code': 'test'})
        self.assertIs(result, failure)
        snapshot.assert_called_once()
        self.assertEqual(result['data']['stdout'], 'retained\n')
        self.assertTrue(result['data']['native_outcome_unknown'])
        self.assertTrue(result['data']['error']['details']['native_details']['native_outcome_unknown'])


if __name__ == '__main__':
    unittest.main()
