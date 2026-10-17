"""Discovery changes preserve schemas, callable names and exact signatures."""
import asyncio
import inspect
import unittest

from solidworks_mcp import server
from solidworks_mcp.shape_program import ShapeProgram
from solidworks_mcp.automation.shape_service import API, SHAPE_INSTRUCTIONS
from solidworks_mcp.tool_lifecycle import DEPRECATED_TOOLS, DEPRECATION_META_KEY, annotate_lifecycle


class ToolLifecycleTests(unittest.TestCase):
    def test_repeat_discovery_is_stable_and_replacements_exist(self):
        first = server._all_tools()
        second = server._all_tools()
        self.assertEqual([t.model_dump() for t in first], [t.model_dump() for t in second])
        self.assertEqual([t.model_dump() for t in first],
                         [t.model_dump() for t in annotate_lifecycle(first)])
        catalog = {t.name: t for t in first}
        for name, entry in DEPRECATED_TOOLS.items():
            self.assertTrue(catalog[name].meta[DEPRECATION_META_KEY]['callable'])
            self.assertTrue(set(entry['replacements']).issubset(catalog))
            self.assertNotIn(name, entry['replacements'])
            self.assertIn('_meta', catalog[name].model_dump(by_alias=True, exclude_none=True))

    def test_deprecated_alias_still_resolves_original_dispatch(self):
        args = {'sketch_name': 'Profile', 'image_path': 'D:/reference.png'}
        self.assertEqual(server._resolve_expert({'action': 'call',
            'name': 'compare_sketch_to_image', 'arguments': args}),
            ('compare_sketch_to_image', args))
        entries = server._resolve_expert({'action': 'list', 'query': 'compare_sketch_to_image'})['data']['tools']
        self.assertTrue(entries[0]['deprecated'])
        self.assertEqual(entries[0]['replacements'], ['compare_sketch_to_reference'])

    def test_tools_made_redundant_by_long_operations_are_deprecated(self):
        public = {t.name for t in asyncio.run(server.list_tools())}
        catalog = {t.name: t for t in server._all_tools()}
        for name, replacement in (('get_job_result', 'wait_operation'), ('execute_python_async', 'execute_python'),
                                  ('list_jobs', 'get_executor_status')):
            with self.subTest(name=name):
                self.assertNotIn(name, public)
                self.assertTrue(catalog[name].description.startswith('DEPRECATED: use ' + replacement))
                entry = catalog[name].meta[DEPRECATION_META_KEY]
                self.assertEqual(entry['catalog_since'], '6.29.4')
                self.assertNotIn('since', entry)
        # Still reachable: the expert catalog resolves it and the handler dispatches the name.
        self.assertEqual(server._resolve_expert({'action': 'call', 'name': 'get_job_result',
                                                 'arguments': {'job_id': 'op1'}}),
                         ('get_job_result', {'job_id': 'op1'}))
        self.assertEqual(catalog['extrude_sketch'].meta[DEPRECATION_META_KEY]['catalog_since'], '6.24.0')
        self.assertIn('wait_operation', public)

    def test_nothing_a_client_reads_recommends_a_deprecated_tool(self):
        live = [t for t in server._all_tools() if t.name not in DEPRECATED_TOOLS] + [server._expert_tool()]
        for tool in live:
            for name in DEPRECATED_TOOLS:
                self.assertNotRegex(tool.description, r'\b' + name + r'\b', (tool.name, name))
        for topic, text in API.items():
            for name in DEPRECATED_TOOLS:
                self.assertNotIn(name, str(text), (topic, name))
        for name in DEPRECATED_TOOLS:
            self.assertNotIn(name, SHAPE_INSTRUCTIONS)

    def test_main_descriptions_name_what_was_added(self):
        public = {t.name: t for t in asyncio.run(server.list_tools())}
        self.assertIn('templates', public['model_api_lookup'].description)
        model = public['execute_model_code']
        for word in ('warnings', 'TUBE_BEND_TOO_TIGHT', 'NATIVE_BODY_INVALID', 'Revert', 'restores the saved part'):
            self.assertIn(word, model.description)
        self.assertIn('mode=revert', model.inputSchema['properties']['idempotency_key']['description'])
        for name in ('observe_model', 'inspect_model', 'export_model'):
            self.assertIn('closed one is opened', public[name].inputSchema['properties']['document_path']['description'])
        self.assertIn('printability', public['inspect_model'].description)
        self.assertIn('native', public['compare_reference'].description)
        self.assertIn('color', public['measure_reference'].description)
        self.assertIn('memory', public['get_executor_status'].description)
        self.assertEqual(len(public), 13)

    def test_lookup_matches_python_argument_kinds_and_defaults(self):
        for name, text in API['constructors'].items():
            method = name.split('.')[1]
            signature = inspect.signature(getattr(ShapeProgram, method))
            signature = signature.replace(parameters=[p.replace(annotation=inspect.Parameter.empty)
                for k, p in signature.parameters.items() if k != 'self'],
                return_annotation=inspect.Signature.empty)
            self.assertTrue(text.startswith(method+str(signature)), (name, signature, text))


if __name__ == '__main__':
    unittest.main()
