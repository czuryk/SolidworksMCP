"""Command batching must never leave application state changed after a read."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from solidworks_mcp.automation.com_utils import command_scope


class CommandScopeTests(unittest.TestCase):
    def test_restore_false_after_success_and_exception(self):
        app = SimpleNamespace(CommandInProgress=False)
        with patch('solidworks_mcp.automation.com_utils.typed', return_value=app):
            with command_scope(app):
                self.assertTrue(app.CommandInProgress)
            self.assertFalse(app.CommandInProgress)
            with self.assertRaisesRegex(ValueError, 'read failed'):
                with command_scope(app):
                    raise ValueError('read failed')
            self.assertFalse(app.CommandInProgress)

    def test_nested_and_preexisting_true_are_preserved(self):
        app = SimpleNamespace(CommandInProgress=True)
        with patch('solidworks_mcp.automation.com_utils.typed', return_value=app):
            with command_scope(app):
                with command_scope(app):
                    self.assertTrue(app.CommandInProgress)
                self.assertTrue(app.CommandInProgress)
            self.assertTrue(app.CommandInProgress)

    def test_failed_restore_is_reported(self):
        class App:
            value = False
            @property
            def CommandInProgress(self):
                return self.value
            @CommandInProgress.setter
            def CommandInProgress(self, value):
                if not value:
                    raise RuntimeError('Restore unavailable')
                self.value = value
        app = App()
        with patch('solidworks_mcp.automation.com_utils.typed', return_value=app):
            with self.assertRaisesRegex(RuntimeError, 'Restore unavailable'):
                with command_scope(app):
                    pass

    def test_unavailable_interface_fails_before_body(self):
        with patch('solidworks_mcp.automation.com_utils.typed', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'Typed ISldWorks'):
                with command_scope(object()):
                    self.fail('Scope body must not execute')


if __name__ == '__main__':
    unittest.main()
