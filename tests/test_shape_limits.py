"""Component boundaries are independent of geometry, edit style and expanded bodies."""

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.shape_program import ProgramError, ShapeProgram, run_program, apply_patch
from solidworks_mcp.shape_limits import component_budget
from solidworks_mcp.shape_draft import draft_components
from solidworks_mcp.shape_responses import compact_analysis
from solidworks_mcp.shape_checks import program_warnings


def rods(count):
    return f'''H=model.parameter("H",5)
def rod(i):
    return model.tube("Rod"+str(i),[(i*3,0,0),(i*3,H,0)],.65)
for i in range({count}):
    rod(i)
'''


class ShapeLimitTests(unittest.TestCase):
    def test_boundaries_and_numerical_draft(self):
        for count in (127,128,129,255,256,257,511,512):
            with self.subTest(count=count):
                program = run_program(rods(count))
                self.assertEqual(len(program['nodes']), count)
                self.assertEqual(len(draft_components(program)), count)
        with self.assertRaises(ProgramError) as caught:
            run_program(rods(513))
        self.assertEqual(caught.exception.code, 'PROGRAM_LIMIT')
        self.assertEqual(caught.exception.details, {
            'limit_kind': 'shape_components', 'existing_components': 512,
            'attempted_components': 513, 'max_components': 512,
            'component': 'Rod512', 'component_kind': 'tube'})

    def test_hash_unchanged_when_limit_changes(self):
        with patch.object(ShapeProgram, 'MAX_COMPONENTS', 128):
            old = run_program(rods(128))
        self.assertEqual(old, run_program(rods(128)))
        source=(Path(__file__).parent/'fixtures/eiffel_regression/source.py').read_text(encoding='utf-8')
        with patch.object(ShapeProgram,'MAX_COMPONENTS',128):
            original=run_program(source)
        self.assertEqual(original,run_program(source))

    def test_documentation_matches_active_limits(self):
        root=Path(__file__).resolve().parents[1]
        self.assertIn(f'**{ShapeProgram.MAX_COMPONENTS} components**',(root/'README.md').read_text(encoding='utf-8'))
        skill=Path.home()/'.codex/skills/solidworks-mcp'
        if skill.is_dir():
            for name in ('SKILL.md','reference/shape-programs.md','reference/troubleshooting.md'):
                self.assertIn(str(ShapeProgram.MAX_COMPONENTS),(skill/name).read_text(encoding='utf-8'))

    def test_consumed_nodes_and_patterns_are_one_each(self):
        code = '''H=model.parameter("H",5)
a=model.tube("Seed",[(0,0,0),(0,H,0)],1)
b=model.pattern("Copies",[a],count=100,step=(1,0,0))
model.combine("All",[a,b])
'''
        program = run_program(code)
        budget = component_budget(program)
        self.assertEqual(budget['component_count'], 3)
        self.assertEqual(budget['remaining_components'], 509)
        self.assertEqual(budget['expanded_complexity']['largest_component_body_instances'], 99)

    def test_create_rejects_before_any_cad_or_checkpoint(self):
        sw = SolidWorksAutomation()
        with patch.object(sw, 'connect', side_effect=AssertionError('CAD before limit check')), \
             patch.object(sw, 'create_new_part', side_effect=AssertionError('CAD before limit check')), \
             patch('solidworks_mcp.automation.shape_service.shutil.copyfile',
                   side_effect=AssertionError('Checkpoint before limit check')):
            result = sw.execute_model_code(rods(513), mode='create', output_path='C:/limit.SLDPRT')
        self.assertFalse(result['success'])
        self.assertEqual(result['data']['error']['code'], 'PROGRAM_LIMIT')
        self.assertFalse(result['data']['error']['details']['native_mutation_started'])

    def test_edit_code_patch_and_parameter_overrides(self):
        self.assertEqual(len(run_program(apply_patch(rods(511), [{'old':'range(511)', 'new':'range(512)'}]))['nodes']),512)
        self.assertEqual(len(run_program(rods(512), {'H':6})['nodes']),512)
        module = 'solidworks_mcp.automation.shape_service.'
        for payload in ({'code':rods(513)}, {'patch':[{'old':'range(512)', 'new':'range(513)'}]}):
            with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                sw=SolidWorksAutomation()
                path=Path(directory)/'part.SLDPRT'; path.write_bytes(b'original')
                record={'source':rods(512), 'program':run_program(rods(512)), 'native':{}}
                stack.enter_context(patch.object(sw,'get_active_doc',return_value=(SimpleNamespace(GetSaveFlag=False),None)))
                stack.enter_context(patch.object(sw,'_get_doc_path',return_value=str(path)))
                stack.enter_context(patch(module+'read_record',return_value=record))
                stack.enter_context(patch(module+'observed_revision',return_value='r1'))
                stack.enter_context(patch(module+'shutil.copyfile',side_effect=AssertionError('Checkpoint before limit')))
                result=sw.execute_model_code(mode='edit',document_path=str(path),expected_revision='r1',**payload)
                self.assertEqual(result['data']['error']['code'],'PROGRAM_LIMIT',result)
                self.assertEqual(path.read_bytes(),b'original')

    def test_lookup_and_compact_response_use_runtime_limit(self):
        sw=SolidWorksAutomation()
        with patch.object(ShapeProgram,'MAX_COMPONENTS', 517):
            for topic in ('index','all','limits'):
                self.assertEqual(sw.model_api_lookup(topic)['data']['limits']['max_components'],517)
            data=sw.execute_model_code(rods(3))['data']
            compact=compact_analysis(data)
            self.assertEqual((compact['component_count'],compact['max_components'],compact['remaining_components']),
                             (3,517,514))

    def test_nested_pattern_warns_and_refuses_unbounded_draft_allocation(self):
        code='''a=model.dome("Seed",1,1)
b=model.pattern("Copies",[a],count=100,step=(3,0,0))
c=model.pattern("Nested",[b],count=100,step=(0,3,0))
'''
        program=run_program(code)
        self.assertIn('EXPANDED_GEOMETRY_HIGH',[w['code'] for w in program_warnings(program)])
        with self.assertRaises(ProgramError) as caught:
            draft_components(program)
        self.assertEqual(caught.exception.code,'DRAFT_COMPLEXITY_LIMIT')

    def test_shared_union_and_finishes_do_not_duplicate_point_storage(self):
        code = '''a=model.dome("A",1,1)
b=model.dome("B",1,1,center=(1,0,0))
u=model.combine("Union",[a,b])
f=model.fillet("Fillet",u,.1)
model.chamfer("Chamfer",f,.1)
'''
        # Two domes retain 832 points each; all three operations alias those arrays.
        with patch('solidworks_mcp.shape_draft.MAX_DRAFT_POINTS', 1664):
            drafted = draft_components(run_program(code))
        self.assertEqual(len(drafted), 5)
        for name in ('Union', 'Fillet', 'Chamfer'):
            self.assertIs(drafted[name]['pieces'][0], drafted['A']['pieces'][0])
            self.assertIs(drafted[name]['pieces'][1], drafted['B']['pieces'][0])
        self.assertEqual(drafted['A']['consumed_by'], 'Union')

    def test_transformed_shared_union_is_charged_before_copying(self):
        prefix = '''a=model.dome("A",1,1)
b=model.dome("B",1,1,center=(1,0,0))
u=model.combine("Union",[a,b])
'''
        for operation in ('model.pattern("Copies",[u],count=2,step=(3,0,0))',
                          'model.mirror("Copies",[u])',
                          'model.place("Copies",[u],frame=model.frame(origin=(3,0,0)))'):
            with self.subTest(operation=operation), \
                 patch('solidworks_mcp.shape_draft.MAX_DRAFT_POINTS', 1664), \
                 self.assertRaises(ProgramError) as caught:
                draft_components(run_program(prefix + operation))
            self.assertEqual(caught.exception.code, 'DRAFT_COMPLEXITY_LIMIT')
            self.assertEqual(caught.exception.details['component'], 'Copies')
            self.assertEqual(caught.exception.details['retained_points'], 1664)
            self.assertEqual(caught.exception.details['attempted_points'], 3328)

    def test_curved_512_union_fits_the_unchanged_draft_budget(self):
        code = '''H=model.parameter("H",5)
parts=[]
for i in range(511):
    parts.append(model.tube("Rod"+str(i),[(i,0,0),(i+.15,H/2,0),(i,H,0)],.65))
model.combine("Frame",parts,expected_bodies=1)
'''
        program = run_program(code)
        self.assertEqual(len(program['nodes']), 512)
        drafted = draft_components(program)
        self.assertEqual(sum(len(piece) for piece in drafted['Frame']['pieces']), 1046528)
        self.assertIs(drafted['Frame']['pieces'][-1], drafted['Rod510']['pieces'][-1])
        self.assertEqual(drafted['Rod510']['consumed_by'], 'Frame')


if __name__=='__main__':
    unittest.main()
