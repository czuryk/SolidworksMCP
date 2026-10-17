"""Modal detection must separate real dialogs from tooltips (changes.md, P1-7)."""
import sys
import types
import unittest
from unittest.mock import patch

from solidworks_mcp import server
from solidworks_mcp.automation import com_utils
from solidworks_mcp.automation.com_utils import detect_modal_dialog, window_blocks_ui

MAIN, PID = 1000, 77


class FakeWindows:
    """Just enough of win32gui/win32process for a deterministic window list."""

    def __init__(self, windows, main_enabled=True):
        # hwnd -> (class, title, owner, children)
        self.windows = {MAIN: ('AfxFrame', 'SOLIDWORKS Premium 2026 - [Part1]', 0, [])}
        self.windows.update(windows)
        self.main_enabled = main_enabled

    def gui(self):
        module = types.SimpleNamespace()
        module.EnumWindows = lambda callback, extra: [callback(h, extra) for h in self.windows]
        module.IsWindowVisible = lambda hwnd: True
        module.IsWindow = lambda hwnd: hwnd in self.windows
        module.GetClassName = lambda hwnd: self.windows.get(hwnd, ('Button',))[0]
        module.GetWindowText = lambda hwnd: self.windows[hwnd][1] if hwnd in self.windows else 'OK'
        module.IsWindowEnabled = lambda hwnd: self.main_enabled if hwnd == MAIN else True
        module.GetWindowRect = lambda hwnd: (0, 0, 79, 20) if hwnd != MAIN else (0, 0, 1920, 1080)
        module.GetWindowLong = lambda hwnd, index: 0x80000000
        module.GetWindow = lambda hwnd, relation: self.windows.get(hwnd, ('', '', 0))[2]
        module.GetParent = lambda hwnd: 0
        module.GetDlgCtrlID = lambda hwnd: 1
        module.EnumChildWindows = lambda hwnd, callback, extra: [
            callback(child, extra) for child in self.windows[hwnd][3]]
        return module

    def process(self):
        return types.SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (1, PID))


def scan(windows, main_enabled=True, fast=False):
    fake = FakeWindows(windows, main_enabled)
    with patch.dict(sys.modules, {'win32gui': fake.gui(), 'win32process': fake.process()}), \
         patch.object(com_utils, '_exe_name_for_pid', return_value='sldworks.exe'), \
         patch.object(com_utils, '_window_owner_chain',
                      side_effect=lambda hwnd, gui: [fake.windows[hwnd][2]] if fake.windows[hwnd][2] else []), \
         patch.dict(com_utils._SW_MAIN_WINDOW_CACHE, {'hwnd': MAIN if fast else 0, 'pid': PID if fast else 0}):
        return detect_modal_dialog(include_controls=not fast)


TOOLTIP = {2000: ('tooltips_class32', '', MAIN, [])}
SAVE_AS = {3000: ('#32770', 'Save As', MAIN, [3001, 3002])}


class ModalDetectionTests(unittest.TestCase):
    def test_rule(self):
        self.assertFalse(window_blocks_ui(True, 'tooltips_class32', '', []))
        self.assertFalse(window_blocks_ui(True, 'SysShadow', '', []))
        self.assertFalse(window_blocks_ui(True, 'Afx:Popup', '', []))
        self.assertFalse(window_blocks_ui(True, 'Afx:Popup', 'Hint', []))
        self.assertTrue(window_blocks_ui(True, 'Afx:Popup', 'Rebuild errors', [{'class': 'Button'}]))
        self.assertTrue(window_blocks_ui(True, '#32770', '', []))
        self.assertTrue(window_blocks_ui(False, 'tooltips_class32', '', []))

    def test_tooltip_over_enabled_main_window_is_not_modal(self):
        info = scan(TOOLTIP)
        self.assertFalse(info['modal'])
        self.assertEqual(info['state'], 'UI_READY')
        self.assertEqual(info['dialogs'], [])
        self.assertTrue(info['main_window_enabled'])
        # Still reported for diagnostics, explicitly as non-blocking.
        self.assertEqual(info['window_details'][0]['class'], 'tooltips_class32')
        self.assertFalse(info['window_details'][0]['blocking'])
        self.assertNotIn('detected_at', info)

    def test_real_dialog_is_modal_and_described(self):
        info = scan({**TOOLTIP, **SAVE_AS})
        self.assertTrue(info['modal'])
        self.assertEqual(info['dialogs'], ['Save As'])
        blocking = [d for d in info['window_details'] if d['blocking']]
        self.assertEqual([d['class'] for d in blocking], ['#32770'])
        self.assertEqual(len(blocking[0]['controls']), 2)

    def test_disabled_main_window_is_modal(self):
        self.assertTrue(scan({}, main_enabled=False)['modal'])
        self.assertTrue(scan(TOOLTIP, main_enabled=False)['modal'])

    def test_fast_scan_ignores_tooltips_but_not_dialogs(self):
        self.assertFalse(scan(TOOLTIP, fast=True)['modal'])
        self.assertTrue(scan(SAVE_AS, fast=True)['modal'])
        self.assertTrue(scan({}, main_enabled=False, fast=True)['modal'])

    def test_tooltip_does_not_block_a_tool_and_dialog_names_itself(self):
        automation = server.sw_automation
        with patch('solidworks_mcp.automation.base.detect_modal_dialog', return_value=scan(TOOLTIP)):
            self.assertIsNone(automation.ui_preflight('observe_model', {}, mutating=True))
        with patch('solidworks_mcp.automation.base.detect_modal_dialog', return_value=scan(SAVE_AS)):
            blocked = automation.ui_preflight('execute_model_code', {}, mutating=True)
        self.assertEqual(blocked['data']['error']['code'], 'MODAL_DIALOG_BLOCKING')
        self.assertEqual(blocked['data']['error']['details']['ui']['dialogs'], ['Save As'])

    def test_environment_diagnostics_are_not_gated_by_modality(self):
        blocked = {'success': False, 'message': 'modal', 'error_name': 'MODAL_DIALOG_BLOCKING'}
        answer = {'success': True, 'message': 'status', 'error_code': 0, 'error_name': 'swSuccess', 'data': {}}
        with patch.object(server.sw_automation, 'ui_preflight', return_value=blocked) as preflight, \
             patch.object(server.sw_automation, 'ui_postflight', side_effect=lambda name, result, **_: result), \
             patch.object(server, '_get_environment_status', return_value=answer):
            result = server._call_tool_direct('get_environment_status', {})
            self.assertTrue(result['success'])
            preflight.assert_not_called()
            self.assertEqual(server._call_tool_direct('get_model_context', {})['error_name'], 'MODAL_DIALOG_BLOCKING')


if __name__ == '__main__':
    unittest.main()
