"""Completed operation metrics remain stable across repeated result fetches."""
import unittest

from solidworks_mcp.session_trace import SessionTrace, session_report


class SessionOutcomeTests(unittest.TestCase):
    def legacy_calls(self):
        return [
            {'tool': 'execute_model_code', 'mode': 'create', 'success': True,
             'running': True, 'operation_id': 'bad'},
            {'tool': 'wait_operation', 'success': False, 'error': 'NATIVE_RECIPE_FAILED', 'operation_id': 'bad'},
            {'tool': 'execute_model_code', 'mode': 'create', 'success': True,
             'running': True, 'operation_id': 'good'},
            {'tool': 'wait_operation', 'success': True, 'running': True, 'operation_id': 'good'},
            {'tool': 'wait_operation', 'success': True, 'operation_id': 'good', 'operation_ms': 96500},
            {'tool': 'get_executor_status', 'success': True},
        ]

    def test_legacy_trace_joins_terminal_outcomes(self):
        report = session_report(self.legacy_calls())
        self.assertEqual(report['mutations_create_edit'], 2)
        self.assertEqual(report['mutations_failed'], 1)
        self.assertEqual(report['mutations_completed'], 2)
        self.assertEqual(report['mutations_pending'], 0)
        self.assertEqual(report['mutations_answered_running'], 2)
        self.assertEqual(report['slowest_operations'], [
            {'tool': 'execute_model_code', 'mode': 'create', 'seconds': 96.5}])
        self.assertEqual(report['recovery_calls'], 0)
        self.assertEqual(report['wait_operation_calls'], 3)
        self.assertEqual(report['status_calls'], 1)

    def test_fetching_completed_results_twice_does_not_change_operation_metrics(self):
        calls = self.legacy_calls()
        before = session_report(calls)
        after = session_report(calls + [calls[1], calls[4]])
        for key in ('mutations_create_edit', 'mutations_failed', 'mutations_completed',
                    'operation_errors_by_code', 'operation_seconds_total', 'slowest_operations'):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(after['wait_operation_calls'], before['wait_operation_calls'] + 2)
        self.assertEqual(after['errors_by_code']['NATIVE_RECIPE_FAILED'], 2)

    def test_running_does_not_claim_completion(self):
        report = session_report(self.legacy_calls()[:1])
        self.assertEqual(report['mutations_completed'], 0)
        self.assertEqual(report['mutations_pending'], 1)
        self.assertEqual(report['mutations_failed'], 0)

    def test_wait_retains_origin_even_when_start_was_not_retained(self):
        trace = SessionTrace()
        trace.record('wait_operation', {'operation_id': 'old'},
            {'success': False, 'error_name': 'NATIVE_RECIPE_FAILED', 'data': {'operation_id': 'old'}},
            1., 100, 0, operation={'tool': 'execute_model_code', 'mode': 'edit'})
        report = session_report(trace.snapshot())
        self.assertEqual(report['mutations_create_edit'], 1)
        self.assertEqual(report['mutations_failed'], 1)

    def test_response_error_does_not_change_successful_mutation_outcome(self):
        trace = SessionTrace()
        trace.record('execute_model_code', {'mode': 'create'}, {
            'success': False, 'error_name': 'RESULT_SERIALIZATION_ERROR', 'data': {
                'operation_id': 'created', 'response_preparation': {
                    'success': False, 'handler_success': True, 'handler_error': None}}}, 1, 100, 0)
        report = session_report(trace.snapshot())
        self.assertEqual(report['mutations_completed'], 1)
        self.assertEqual(report['mutations_failed'], 0)
        self.assertEqual(report['response_serialization_failures'], 1)
        self.assertEqual(report['operation_errors_by_code'], {})

    def test_recovery_and_status_are_separate(self):
        report = session_report([{'tool': tool, 'success': True} for tool in (
            'wait_operation', 'get_executor_status', 'reconcile_session', 'restart_solidworks')])
        self.assertEqual(report['recovery_calls'], 2)
        self.assertEqual(report['status_calls'], 1)
        self.assertEqual(report['wait_operation_calls'], 1)


if __name__ == '__main__':
    unittest.main()
