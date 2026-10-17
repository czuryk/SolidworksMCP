"""Offline contracts of model.pattern: validation, draft, native recipe and edit planning."""
import unittest

from solidworks_mcp.shape_draft import draft_extents
from solidworks_mcp.shape_equations import plan_recipes, topology
from solidworks_mcp.shape_program import ProgramError, run_program
from solidworks_mcp.shape_reconcile import hidden_host_consumers, reconcile_plan

HEAD = 'H = model.parameter("H", 100)\nu = H / 100\npin = model.tube("Pin", [(20*u, 0, 0), (20*u, 10*u, 0)], 2*u)\n'
ROW = HEAD + 'model.pattern("Row", [pin], count=4, step=(-6*u, 0, 3*u))\n'
RING = HEAD + 'model.pattern("Ring", [pin], count=5, axis="Y")\n'


def plan(code):
    program = run_program(code)
    return plan_recipes(program, {k: f'D{i+1}@ShapeParameters' for i, k in enumerate(program['parameters'])})


class PatternTests(unittest.TestCase):
    def test_arguments_are_validated_before_cad(self):
        for call in ('count=1, step=(1, 0, 0)', 'count=3', 'count=3, step=(1, 0, 0), axis="Y"',
                     'count=3, step=(0, 0, 0)', 'count=3, axis="W"', 'count=2.5, step=(1, 0, 0)',
                     'count=101, step=(1, 0, 0)'):
            with self.subTest(call=call), self.assertRaises(ProgramError) as raised:
                run_program(f'model.pattern("P", [model.dome("D", 5, 5)], {call})')
            self.assertEqual(raised.exception.code, 'INVALID_PATTERN')
        with self.assertRaises(ProgramError):
            run_program('H = model.parameter("H", 4)\nmodel.pattern("P", [model.dome("D", 5, 5)], count=H, step=(1, 0, 0))')

    def test_the_component_holds_the_copies_and_the_seed_stays(self):
        extents = draft_extents(run_program(ROW))
        self.assertEqual(sorted(extents['bodies']), ['Pin', 'Row'])
        # Three copies, each one more step from the seed at x 18..22, z -2..2.
        self.assertEqual(extents['bodies']['Row'], [0.0, 0.0, 1.0, 16.0, 10.0, 11.0])
        ring = draft_extents(run_program(RING))['bodies']['Ring']
        # Right-handed about +Y: the first copy of a seed on +X turns towards -Z.
        self.assertEqual(ring, [-18.18, 0.0, -21.02, 8.18, 10.0, 21.02])
        united = run_program(ROW + 'model.combine("Comb", ["Pin", "Row"], expected_bodies=4)\n')
        self.assertEqual(sorted(draft_extents(united)['bodies']), ['Comb'])

    def test_a_row_costs_one_spacing_dimension_and_a_circle_none(self):
        row = plan(ROW)[-1]
        self.assertEqual(row['variant'], 'row:4')
        self.assertEqual(row['pattern'], {'mode': 'row', 'count': 4, 'spacing': 45 ** .5})
        self.assertEqual(list(row['dimensions']), ['spacing'])
        self.assertIn('sqr(', row['dimensions']['spacing'])
        self.assertTrue(row['sketches'][0]['chord'])
        self.assertEqual(row['sketches'][0]['curves'][0]['xyz'][1], '0')
        ring = plan(RING)[-1]
        self.assertEqual(ring['variant'], 'circle:Y:5')
        self.assertNotIn('dimensions', ring)
        self.assertEqual(ring['sketches'][0]['curves'][0]['xyz'], ['0', '(10*t+0)', '0'])

    def test_step_is_a_definition_edit_and_count_a_topology_edit(self):
        old = plan(ROW)
        moved = plan(ROW.replace('-6*u', '-7*u'))
        self.assertEqual(topology(old), topology(moved))
        more = reconcile_plan(old, plan(ROW.replace('count=4', 'count=6')))
        self.assertEqual(more['build'], ['Row'])
        self.assertEqual(more['preserve'], ['Pin'])

    def test_a_new_copy_of_a_consumed_seed_rebuilds_its_consumer(self):
        saved = HEAD + 'base = model.dome("Base", 30*u, 5*u)\nmodel.combine("Body", [base, pin])\n'
        edited = HEAD + ('base = model.dome("Base", 30*u, 5*u)\nrow = model.pattern("Row", [pin], count=3, step=(0, 0, 8*u))\n'
                         'model.combine("Body", [base, pin])\n')
        new = plan(edited)
        self.assertEqual(hidden_host_consumers(new, {'Row'}), {'Body'})
        self.assertEqual(reconcile_plan(plan(saved), new)['build'], ['Row', 'Body'])
        mirrored = plan(HEAD + 'base = model.dome("Base", 30*u, 5*u)\nmodel.mirror("PinR", [pin])\n'
                               'model.combine("Body", [base, pin])\n')
        self.assertEqual(hidden_host_consumers(mirrored, {'PinR'}), {'Body'})


if __name__ == '__main__':
    unittest.main()
