"""Editing the embedded program by exact replacements (changes.md, P1-3)."""
import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.automation.shape_service import edited_program
from solidworks_mcp.shape_program import ProgramError, apply_patch, changed_line_count, run_program
from solidworks_mcp.shape_tools import SHAPE_TOOLS
from solidworks_mcp.tool_registry import dispatch_new_tool

MODULE = 'solidworks_mcp.automation.shape_service.'
FIXTURE = (Path(__file__).parent / 'fixtures' / 'bender_program.py').read_text(encoding='utf-8')
SOURCE = ('H = model.parameter("H", 150)\n'
          'u = H / 150\n'
          'a = model.dome("A", 4*u, 2*u)\n'
          'b = model.dome("B", 4*u, 2*u, center=(20*u, 0, 0))\n'
          'model.combine("C", [a, model.dome("Bridge", 12*u, 2*u, center=(10*u, 0, 0)), b])\n')


def code_of(error):
    return error.exception.code


class ApplyPatchTests(unittest.TestCase):
    def test_single_match_is_replaced(self):
        self.assertEqual(apply_patch('a = 1\nb = 2\n', [{'old': 'b = 2', 'new': 'b = 3'}]), 'a = 1\nb = 3\n')

    def test_missing_and_ambiguous_old_are_rejected(self):
        with self.assertRaises(ProgramError) as missing:
            apply_patch(SOURCE, [{'old': 'model.tube', 'new': 'x'}])
        self.assertEqual(code_of(missing), 'PATCH_NOT_FOUND')
        self.assertEqual(missing.exception.details['index'], 0)
        with self.assertRaises(ProgramError) as twice:
            apply_patch(SOURCE, [{'old': '4*u, 2*u', 'new': '5*u, 2*u'}])
        self.assertEqual(code_of(twice), 'PATCH_NOT_UNIQUE')
        self.assertEqual(twice.exception.details['matches'], 2)

    def test_empty_new_deletes_and_order_matters(self):
        self.assertEqual(apply_patch('keep\ndrop\n', [{'old': 'drop\n', 'new': ''}]), 'keep\n')
        # The second replacement sees the text produced by the first one.
        ordered = apply_patch('x = 1\n', [{'old': 'x = 1', 'new': 'y = 2'}, {'old': 'y = 2', 'new': 'z = 3'}])
        self.assertEqual(ordered, 'z = 3\n')
        with self.assertRaises(ProgramError) as reversed_order:
            apply_patch('x = 1\n', [{'old': 'y = 2', 'new': 'z = 3'}, {'old': 'x = 1', 'new': 'y = 2'}])
        self.assertEqual(code_of(reversed_order), 'PATCH_NOT_FOUND')
        # A later failure reports its own step.
        with self.assertRaises(ProgramError) as second:
            apply_patch('x = 1\n', [{'old': 'x = 1', 'new': 'y = 2'}, {'old': 'x = 1', 'new': 'q'}])
        self.assertEqual(second.exception.details['index'], 1)

    def test_malformed_patches_are_rejected(self):
        for bad in ([], 'x', [{'old': 'a'}], [{'old': '', 'new': 'a'}], [{'old': 1, 'new': 'a'}],
                    [{'old': 'a', 'new': 'b', 'extra': 1}], [{'old': 'a', 'new': 'a'}] * 65):
            with self.assertRaises(ProgramError) as raised:
                apply_patch('a', bad)
            self.assertEqual(code_of(raised), 'INVALID_PATCH')

    def test_changed_line_count(self):
        self.assertEqual(changed_line_count('a\nb\nc\n', 'a\nB\nc\n'), 1)
        self.assertEqual(changed_line_count('a\nb\nc\n', 'a\nc\n'), 1)
        self.assertEqual(changed_line_count('a\n', 'a\n'), 0)

    def test_reference_one_number_edit_is_a_small_call(self):
        # changes.md quotes 27.8; the saved reference program has the 17.8 mm middle prism.
        change = [{'old': '17.8*u, 12.0*u', 'new': '17.8*u, 11.0*u'}]
        patched = apply_patch(FIXTURE, change)
        self.assertEqual(changed_line_count(FIXTURE, patched), 1)
        call = {'mode': 'edit', 'document_path': 'D:\\TMP\\retest-01\\work\\Bender.SLDPRT',
                'expected_revision': 'a' * 64, 'patch': change}
        self.assertLessEqual(len(json.dumps(call)), 300)
        self.assertGreater(len(FIXTURE), 9000)


class EditedProgramTests(unittest.TestCase):
    def record(self, values=None):
        return {'source': SOURCE, 'program': run_program(SOURCE, values)}

    def test_patch_equals_the_same_edit_as_a_full_program(self):
        change = [{'old': '12*u, 2*u', 'new': '13*u, 2*u'}]
        full = SOURCE.replace('12*u, 2*u', '13*u, 2*u')
        _, by_patch, lines = edited_program(self.record(), None, change, None, lambda: {'H': 150.0})
        _, by_code, _ = edited_program(self.record(), full, None, None, lambda: self.fail('Not needed for code'))
        self.assertEqual(by_patch['program_hash'], by_code['program_hash'])
        self.assertNotEqual(by_patch['program_hash'], self.record()['program']['program_hash'])
        self.assertEqual(lines, 1)

    def test_patch_keeps_current_native_parameter_values(self):
        change = [{'old': '12*u, 2*u', 'new': '13*u, 2*u'}]
        _, program, _ = edited_program(self.record({'H': 120}), None, change, None, lambda: {'H': 120.0})
        self.assertEqual(program['parameters']['H']['value'], 120.0)
        _, program, _ = edited_program(self.record({'H': 120}), None, change, {'H': 90}, lambda: {'H': 120.0})
        self.assertEqual(program['parameters']['H']['value'], 90.0)

    def test_patched_default_wins_over_the_old_native_value(self):
        change = [{'old': '"H", 150)', 'new': '"H", 160)'}]
        _, program, _ = edited_program(self.record(), None, change, None, lambda: {'H': 150.0})
        self.assertEqual(program['parameters']['H']['value'], 160.0)

    def test_parameter_only_edit_reads_native_values(self):
        code, program, lines = edited_program(self.record(), None, None, {'H': 120}, lambda: {'H': 150.0})
        self.assertEqual(code, SOURCE)
        self.assertEqual(program['parameters']['H']['value'], 120.0)
        self.assertEqual(lines, 0)


class ServiceTests(unittest.TestCase):
    def test_schema_offers_patch(self):
        tool = next(tool for tool in SHAPE_TOOLS if tool.name == 'execute_model_code')
        self.assertEqual(tool.inputSchema['properties']['patch']['items']['required'], ['old', 'new'])

    def test_code_and_patch_are_mutually_exclusive_and_patch_needs_a_document(self):
        sw = SolidWorksAutomation()
        both = sw.execute_model_code(SOURCE, mode='edit', patch=[{'old': 'a', 'new': 'b'}])
        self.assertEqual(both['data']['error']['code'], 'INVALID_ARGUMENT')
        create = sw.execute_model_code(mode='create', output_path='C:/new.SLDPRT', patch=[{'old': 'a', 'new': 'b'}])
        self.assertEqual(create['data']['error']['code'], 'INVALID_ARGUMENT')
        bare = sw.execute_model_code(mode='analyze', patch=[{'old': 'a', 'new': 'b'}])
        self.assertEqual(bare['data']['error']['code'], 'INVALID_ARGUMENT')

    def test_rejected_patch_leaves_the_document_untouched(self):
        sw = SolidWorksAutomation()
        doc = SimpleNamespace(GetSaveFlag=False)
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = Path(directory) / 'part.SLDPRT'
            path.write_bytes(b'unchanged')
            record = {'source': SOURCE, 'program': run_program(SOURCE), 'native': {}}
            stack.enter_context(patch.object(sw, 'get_active_doc', return_value=(doc, None)))
            stack.enter_context(patch.object(sw, '_get_doc_path', return_value=str(path)))
            stack.enter_context(patch(MODULE + 'read_record', return_value=record))
            stack.enter_context(patch(MODULE + 'observed_revision', return_value='r1'))
            stack.enter_context(patch(MODULE + 'global_values', return_value={'H': 150.0}))
            copier = stack.enter_context(patch(MODULE + 'shutil.copyfile'))
            updater = stack.enter_context(patch(MODULE + 'update_native'))
            for change, expected in (([{'old': 'model.tube(', 'new': 'x'}], 'PATCH_NOT_FOUND'),
                                     ([{'old': '4*u, 2*u', 'new': '5*u, 2*u'}], 'PATCH_NOT_UNIQUE')):
                result = sw.execute_model_code(mode='edit', document_path=str(path),
                                               expected_revision='r1', patch=change)
                error = result['data']['error']
                self.assertEqual(error['code'], expected)
                self.assertFalse(error['details']['native_mutation_started'])
                self.assertNotIn('traceback', error['details'])
            copier.assert_not_called()
            updater.assert_not_called()
            self.assertEqual(path.read_bytes(), b'unchanged')

    def plan(self, **arguments):
        """Run analyze against a sidecar, as the COM-free MCP path does."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'part.SLDPRT'
            path.write_bytes(b'native bytes')
            program = run_program(SOURCE)
            record = {'source': SOURCE, 'program': program,
                      'native': {'parameter_bindings': {'H': {'dimension': 'D1@ShapeParameters', 'unit': 'mm'}},
                                 'ownership': {'A': {}}, 'feature_inventory': {'A': 'Revolution'}},
                      'native_file_sha256': hashlib.sha256(b'native bytes').hexdigest()}
            if arguments.pop('stale', False):
                record['native_file_sha256'] = 'stale'
            if not arguments.pop('no_sidecar', False):
                path.with_suffix('.shape.json').write_text(json.dumps(record), encoding='utf-8')
            return dispatch_new_tool(SolidWorksAutomation(), 'execute_model_code',
                                     {'mode': 'analyze', 'document_path': str(path), **arguments})

    def test_plan_of_a_definition_patch_rebuilds_nothing(self):
        result = self.plan(patch=[{'old': '12*u, 2*u', 'new': '13*u, 2*u'}])
        self.assertTrue(result['success'], result)
        plan = result['data']['plan']
        self.assertEqual(plan['would_rebuild'], [])
        self.assertEqual(plan['would_add'], [])
        self.assertEqual(plan['would_remove'], [])
        self.assertEqual(plan['preserved_count'], 4)
        self.assertEqual(plan['edit_strategy'], 'definition_update')
        self.assertEqual(result['data']['changed_source_lines'], 1)
        self.assertTrue(result['data']['patch_applied'])

    def test_plan_of_a_topology_change_lists_dependents(self):
        change = [{'old': 'a = model.dome("A", 4*u, 2*u)',
                   'new': 'a = model.ellipsoid("A", (4*u, 2*u, 4*u))\nmodel.dome("Extra", 2*u, 1*u, center=(0, 20*u, 0))'}]
        plan = self.plan(patch=change)['data']['plan']
        self.assertEqual(plan['would_rebuild'], ['A', 'C'])
        self.assertEqual(plan['would_add'], ['Extra'])
        self.assertEqual(plan['edit_strategy'], 'dependency_reconcile')
        removed = self.plan(code=SOURCE.replace('a = model.dome("A", 4*u, 2*u)\n', '').replace('[a, ', '['))
        self.assertEqual(removed['data']['plan']['would_remove'], ['A'])

    def test_plan_needs_a_current_sidecar(self):
        change = [{'old': '12*u, 2*u', 'new': '13*u, 2*u'}]
        self.assertEqual(self.plan(patch=change, no_sidecar=True)['data']['error']['code'], 'PLAN_SOURCE_UNAVAILABLE')
        self.assertEqual(self.plan(patch=change, stale=True)['data']['error']['code'], 'PLAN_SOURCE_STALE')
        self.assertEqual(self.plan(patch=[{'old': 'missing', 'new': 'x'}])['data']['error']['code'], 'PATCH_NOT_FOUND')


if __name__ == '__main__':
    unittest.main()
