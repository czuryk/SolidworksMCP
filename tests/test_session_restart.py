"""Recovery after the MCP owner process is lost."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from solidworks_mcp import server
from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.automation.runtime import atomic_json_write
from solidworks_mcp.automation.sta_executor import StaExecutor

MODULE = 'solidworks_mcp.automation.session_restart.'


def ok(**data):
    return {'success': True, 'message': 'test', 'error_code': 0, 'error_name': 'swSuccess', 'data': data}


class RestartTests(unittest.TestCase):
    """restart_solidworks protects unsaved work and proves the process changed."""

    def setUp(self):
        self.old = [{'pid': 100, 'created_at': 10.0}]
        self.new = [{'pid': 200, 'created_at': 99.0}]
        self.alive = True
        self.sw = SolidWorksAutomation()
        self.saved = SimpleNamespace(title='Saved.SLDPRT', path='C:\\work\\Saved.SLDPRT', GetSaveFlag=False)
        self.dirty = SimpleNamespace(title='Foreign.SLDPRT', path='C:\\other\\Foreign.SLDPRT', GetSaveFlag=True)
        self.untitled = SimpleNamespace(title='Part7', path='', GetSaveFlag=False)
        self.documents = [self.saved]
        self.app = SimpleNamespace(RevisionNumber='34.2.1', StartupProcessCompleted=True,
                                   CloseAllDocuments=Mock(), ExitApp=Mock(side_effect=self.exit_app))
        self.app.GetDocuments = lambda: list(self.documents)
        self.started = []
        patches = [
            patch(MODULE + 'solidworks_processes', side_effect=lambda: self.old if self.alive else self.new),
            patch(MODULE + 'process_created_at',
                  side_effect=lambda pid: 10.0 if (pid == 100 and self.alive) else None),
            patch(MODULE + 'process_image_path', return_value='C:\\SW\\SLDWORKS.exe'),
            patch(MODULE + 'os.path.exists', return_value=True),
            patch(MODULE + 'os.startfile', side_effect=self.started.append, create=True),
            patch.object(self.sw, '_try_connect_com', side_effect=self.attach),
            patch.object(self.sw, '_get_doc_path', side_effect=lambda doc: doc.path),
            patch.object(self.sw, '_get_doc_title', side_effect=lambda doc: doc.title),
            patch.object(self.sw, 'connect', return_value=ok()),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def attach(self):
        self.sw._sw_app = self.app
        return True

    def exit_app(self):
        self.alive = False

    def test_saved_documents_restart_and_report_both_pids(self):
        result = self.sw.restart_solidworks(exit_timeout_sec=1, startup_timeout_sec=1)
        self.assertTrue(result['success'], result)
        data = result['data']
        self.assertEqual((data['previous_pids'], data['new_pids']), ([100], [200]))
        self.assertTrue(data['restarted'])
        self.assertEqual(data['closed_documents'][0]['path'], 'C:\\work\\Saved.SLDPRT')
        self.assertEqual(data['discarded_unsaved'], [])
        # COM activation starts the one instance; the executable is never launched beside it.
        self.assertEqual(self.started, [])
        self.app.ExitApp.assert_called_once()

    def test_unsaved_or_untitled_documents_refuse_without_the_flag(self):
        for foreign in (self.dirty, self.untitled):
            self.documents = [self.saved, foreign]
            result = self.sw.restart_solidworks(exit_timeout_sec=1, startup_timeout_sec=1)
            error = result['data']['error']
            self.assertEqual(error['code'], 'UNSAVED_DOCUMENTS')
            self.assertEqual([d['title'] for d in error['details']['unsaved_documents']], [foreign.title])
            self.assertFalse(error['details']['restarted'])
        self.app.ExitApp.assert_not_called()
        self.app.CloseAllDocuments.assert_not_called()
        self.assertEqual(self.started, [])
        self.assertTrue(self.alive)

    def test_explicit_discard_closes_everything_and_names_what_was_lost(self):
        self.documents = [self.saved, self.dirty]
        result = self.sw.restart_solidworks(discard_unsaved=True, exit_timeout_sec=1, startup_timeout_sec=1)
        self.assertTrue(result['success'], result)
        self.assertEqual([d['title'] for d in result['data']['discarded_unsaved']], ['Foreign.SLDPRT'])
        self.app.CloseAllDocuments.assert_called_once_with(True)

    def test_unresponsive_solidworks_is_not_killed_without_the_flag(self):
        with patch.object(self.sw, '_try_connect_com', return_value=False):
            result = self.sw.restart_solidworks(exit_timeout_sec=1, startup_timeout_sec=1)
        self.assertEqual(result['data']['error']['code'], 'SOLIDWORKS_UNRESPONSIVE')
        self.assertEqual(self.started, [])

    def test_process_that_does_not_exit_is_reported(self):
        self.app.ExitApp = Mock()
        result = self.sw.restart_solidworks(exit_timeout_sec=.2, startup_timeout_sec=1)
        self.assertEqual(result['data']['error']['code'], 'SOLIDWORKS_EXIT_TIMEOUT')
        self.assertEqual(self.started, [])


class QuarantinedSessionTests(unittest.TestCase):
    """The restart runs in a quarantined session and reconciliation then passes."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.identities = [{'pid': 100, 'created_at': time.time() - 500}]
        self.executor = StaExecutor(Path(self.temp.name) / 'session.lock',
                                    identity_provider=lambda: self.identities,
                                    initialize=lambda: None, finalize=lambda: None, pump=lambda: None)
        self.addCleanup(self.executor.close)
        atomic_json_write(self.executor.state_path, {
            'status': 'RUNNING', 'operation_id': 'orphan', 'tool': 'execute_model_code',
            'owner_pid': 999999, 'owner_started_at': 1.0, 'started_at': time.time() - 60,
            'solidworks_processes': list(self.identities)})

    def restart(self, **_):
        self.identities = [{'pid': 200, 'created_at': time.time() + 1}]
        return ok(restarted=True, previous_pids=[100], new_pids=[200])

    def test_status_and_error_explain_the_orphan(self):
        status = self.executor.status()
        self.assertTrue(status['quarantined'])
        self.assertIs(status['owner_alive'], False)
        self.assertEqual(status['cause'], 'MCP_OWNER_PROCESS_EXITED')
        self.assertEqual(status['recovery']['tool'], 'restart_solidworks')
        self.assertEqual(status['recovery']['then']['arguments'], {'operation_id': 'orphan'})
        early = self.executor.submit('reconcile_session', {}, lambda _: self.fail('No restart yet'),
                                     recovery_id='orphan').response.result(1)
        self.assertEqual(early['error_name'], 'SESSION_QUARANTINED')
        self.assertIn('restart_solidworks', early['message'])

    def test_restart_through_the_server_then_reconcile(self):
        async def scenario():
            blocked = await server.call_tool('get_document_info', {})
            self.assertEqual(blocked.structuredContent['error_name'], 'SESSION_QUARANTINED')
            restarted = await server.call_tool('expert_tools', {'action': 'call', 'name': 'restart_solidworks'})
            self.assertFalse(restarted.isError, restarted.structuredContent)
            self.assertEqual(restarted.structuredContent['data']['new_pids'], [200])
            # The orphan record is still the journal: maintenance must not replace it.
            self.assertEqual(self.executor.status()['journal']['operation_id'], 'orphan')
            self.assertTrue(self.executor.status()['quarantined'])
            reconciled = await server.call_tool('reconcile_session', {'operation_id': 'orphan'})
            self.assertFalse(reconciled.isError, reconciled.structuredContent)
            self.assertTrue(reconciled.structuredContent['data']['reconciliation']['solidworks_restart_verified'])
            after = await server.call_tool('get_document_info', {})
            self.assertFalse(after.isError)
        with patch.object(server, 'cad_executor', self.executor), \
             patch.object(server.sw_automation, 'restart_solidworks', side_effect=self.restart), \
             patch.object(server, '_reconcile_native', side_effect=lambda restarted: ok(
                 reconciliation={'solidworks_restart_verified': restarted})), \
             patch.object(server, '_call_tool_direct', return_value=ok()):
            asyncio.run(scenario())
        self.assertEqual(self.executor.status()['journal']['status'], 'COMPLETED')
        self.assertFalse(self.executor.status()['quarantined'])

    def test_restart_arguments_are_validated(self):
        with patch.object(server, 'cad_executor', self.executor), \
             patch.object(server.sw_automation, 'restart_solidworks', side_effect=self.restart) as restart:
            bad = asyncio.run(server.call_tool('restart_solidworks', {'discard_unsaved': 'yes'}))
            self.assertEqual(bad.structuredContent['error_name'], 'INVALID_ARGUMENT')
            restart.assert_not_called()
            asyncio.run(server.call_tool('restart_solidworks', {'discard_unsaved': True}))
            restart.assert_called_once_with(discard_unsaved=True)

    def test_mutation_journals_its_checkpoint(self):
        atomic_json_write(self.executor.state_path, {'status': 'COMPLETED', 'operation_id': 'previous'})
        seen = {}

        def edit():
            self.assertTrue(self.executor.annotate(checkpoint='C:\\tmp\\shape-edit-1.SLDPRT',
                                                   source_sha256='a' * 64, starting_revision='r1'))
            seen.update(self.executor.status()['journal'])
            return ok()
        self.assertTrue(self.executor.submit('execute_model_code', {}, edit).response.result(2)['success'])
        self.assertEqual(seen['status'], 'RUNNING')
        self.assertEqual(seen['checkpoint'], 'C:\\tmp\\shape-edit-1.SLDPRT')
        self.assertEqual((seen['source_sha256'], seen['starting_revision']), ('a' * 64, 'r1'))
        self.assertFalse(self.executor.annotate(checkpoint='late'))


if __name__ == '__main__':
    unittest.main()
