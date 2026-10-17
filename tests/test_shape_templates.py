"""Offline checks of the template library: every template compiles and is reachable by lookup."""
import unittest

from solidworks_mcp import server
from solidworks_mcp.shape_draft import draft_extents
from solidworks_mcp.shape_equations import plan_recipes
from solidworks_mcp.shape_program import run_program
from solidworks_mcp.shape_templates import INDEX, TEMPLATES
from solidworks_mcp.tool_registry import dispatch_new_tool


def lookup(topic):
    return dispatch_new_tool(server.sw_automation, 'model_api_lookup', {'topic': topic})


class TemplateTests(unittest.TestCase):
    def test_every_template_compiles_to_its_bodies(self):
        for name, item in TEMPLATES.items():
            with self.subTest(template=name):
                program = run_program(item['code'])
                plan_recipes(program, {k: f'D{i+1}@ShapeParameters' for i, k in enumerate(program['parameters'])})
                self.assertEqual(sorted(draft_extents(program)['bodies']), sorted(item['bodies']))
                for pair in item['fits']:
                    self.assertTrue(set(pair) <= set(item['bodies']))
                # A fit is defined by the native clearance parameter, so it survives scaling.
                self.assertEqual('Clr' in program['parameters'], bool(item['fits']))
                self.assertLessEqual(len(item['code']), 1800)

    def test_lookup_lists_and_returns_templates(self):
        listing = lookup('templates')
        self.assertTrue(listing['success'])
        self.assertEqual(listing['data']['templates'], INDEX)
        for topic in ('template:hinge', 'template_hinge'):
            answer = lookup(topic)
            self.assertTrue(answer['success'], topic)
            self.assertEqual(answer['data']['code'], TEMPLATES['hinge']['code'])
            self.assertEqual(answer['data']['clearance_pairs'], [['LeafA', 'LeafB']])
        self.assertIn('templates', lookup('index')['data']['topics'])
        everything = lookup('all')['data']
        self.assertEqual(set(everything['templates']) - {'usage'}, set(TEMPLATES))
        self.assertNotIn('model.combine', str(everything['templates']))     # The index carries no code.

    def test_unknown_template_suggests_the_closest(self):
        answer = lookup('template:hinges')
        self.assertFalse(answer['success'])
        self.assertIn('template:hinge', answer['data']['error']['details']['did_you_mean'])


if __name__ == '__main__':
    unittest.main()
