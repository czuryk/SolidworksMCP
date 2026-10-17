"""Unknown native outputs, containment and scope must not certify a clearance."""
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from solidworks_mcp.automation import body_relations as rel
from solidworks_mcp.shape_program import ProgramError


class Body:
    def __init__(self, volume=100., error=0, output=None):
        self.volume, self.error, self.output = volume, error, output
        self.Name = 'Fixture'
        self.copies = 0

    def Check3(self):
        return SimpleNamespace(Count=0)

    def GetMassProperties(self, density):
        return [0, 0, 0, self.volume/1e9]

    def Copy2(self, *args):
        self.copies += 1
        return Body(self.volume, self.error, self.output)

    def Operations2(self, operation, other, error):
        return self.output, self.error


class BodyRelationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(rel, 'typed', side_effect=lambda obj, _: obj))
        self.enterContext(patch.object(rel, 'com_get', side_effect=lambda obj, name, *args:
            getattr(obj, name)(*args) if callable(getattr(obj, name)) else getattr(obj, name)))
        self.enterContext(patch.object(rel, 'resolve_solidworks_constant', return_value=15901))

    def measure(self, distance=.25, volume=0, failure=None, query=None):
        with patch.object(rel, '_distance', return_value={'distance_mm': distance, 'points_mm': [], 'method': 'test'}), \
             patch.object(rel, 'native_intersection', return_value={'volume_mm3': volume, 'method': 'test'}, side_effect=failure):
            return rel.measure_pair(None, None, None, query or {'min_clearance_mm': .2})

    def test_containment_positive_boundary_distance_is_not_clearance(self):
        result = self.measure(3, 33.5)
        self.assertEqual(result['boundary_distance_mm'], 3)
        self.assertEqual(result['relation'], 'OVERLAP')
        self.assertEqual(result['clearance_mm'], 0)
        self.assertFalse(result['clearance_check']['passed'])

    def test_native_failure_never_passes_even_with_positive_distance(self):
        result = self.measure(failure=ProgramError('BODY_BOOLEAN_FAILED', 'failed', native_error=1058))
        self.assertIsNone(result['clearance_check']['passed'])
        self.assertIsNone(result['clearance_mm'])
        self.assertEqual(result['relation'], 'UNKNOWN')
        self.assertEqual(result['errors'][0]['details']['native_error'], 1058)

    def test_distance_failure_does_not_turn_no_intersection_into_pass(self):
        with patch.object(rel, '_distance', side_effect=ValueError('no solution')), \
             patch.object(rel, 'native_intersection', return_value={'volume_mm3': 0, 'method': 'test'}):
            result = rel.measure_pair(None, None, None, {'min_clearance_mm': .2})
        self.assertEqual(result['relation'], 'UNKNOWN')
        self.assertIsNone(result['clearance_check']['passed'])

    def test_overlap_still_fails_when_distance_unknown(self):
        with patch.object(rel, '_distance', side_effect=ValueError()), \
             patch.object(rel, 'native_intersection', return_value={'volume_mm3': .0000001, 'method': 'test'}):
            result = rel.measure_pair(None, None, None, {'min_clearance_mm': 0})
        self.assertFalse(result['clearance_check']['passed'])
        self.assertEqual(result['relation'], 'OVERLAP')

    def test_gap_interval_contact_and_unrounded_value(self):
        self.assertTrue(self.measure(query={'min_clearance_mm': .2, 'max_clearance_mm': .3})['clearance_check']['passed'])
        self.assertFalse(self.measure(.30001, query={'max_clearance_mm': .3})['clearance_check']['passed'])
        result = self.measure(.000002)
        self.assertEqual(result['relation'], 'CONTACT_OR_NEAR')
        self.assertEqual(result['clearance_mm'], .000002)
        self.assertFalse(result['clearance_check']['passed'])

    def test_nonzero_boolean_error_with_empty_result_is_unknown(self):
        for error in [2, 5, 6, 1058, 1067]:
            body = Body(error=error)
            with self.assertRaises(ProgramError):
                rel.native_intersection(body, Body(), .25)
            self.assertEqual(body.copies, 1)

    def test_kernel_fault_prevents_boolean_operation(self):
        body = Body()
        body.Check3 = lambda: SimpleNamespace(Count=1)
        with self.assertRaises(ProgramError): rel.native_intersection(body, Body(), .25)
        self.assertEqual(body.copies, 0)

    def test_legacy_unknown_cannot_pass_requested_clearance(self):
        from solidworks_mcp.automation.bodies import BodyOperations
        fake = SimpleNamespace(
            _require_part=lambda: (object(), None),
            _find_body=lambda doc, name: Body(),
            _units=SimpleNamespace(from_meters=lambda value,unit:1000, default_unit=SimpleNamespace(value='mm')),
            app=None, _error=lambda *args: {'success':False},
            _result=lambda ok,msg,code,data: {'success':ok,'data':data})
        unknown={'relation':'UNKNOWN','boundary_distance_mm':.25,'clearance_mm':None,
                 'intersection_volume_mm3':None,'interference':None,'clearance_check':{'passed':None}}
        with patch.object(rel,'measure_pair',return_value=unknown), \
             patch('solidworks_mcp.automation.com_utils.command_scope',return_value=nullcontext()), \
             patch('solidworks_mcp.automation.shape_observation.geometry_state',return_value={}), \
             patch('solidworks_mcp.automation.shape_observation.ensure_unchanged'), \
             patch('solidworks_mcp.automation.bodies.com_get',return_value=False):
            result=BodyOperations.check_clearance(fake,'A','B',.2,'mm')
        self.assertFalse(result['success'])
        self.assertIsNone(result['data']['clearance_ok'])

    def test_missing_mass_properties_not_counted_as_zero(self):
        body = Body(output=[Body(volume=float('nan'))])
        with self.assertRaises(ProgramError):
            rel.native_intersection(body, Body(), .25)

    def test_empty_contact_boolean_requires_cut_confirmation(self):
        with patch.object(rel, '_operation', side_effect=[[], [100.]]):
            self.assertEqual(rel.native_intersection(Body(), Body(), 0)['volume_mm3'], 0)
        with patch.object(rel, '_operation', side_effect=[[], []]):
            self.assertEqual(rel.native_intersection(Body(), Body(), 0)['volume_mm3'], 100)
        with patch.object(rel, '_operation', side_effect=[[], [99.]]):
            with self.assertRaises(ProgramError):
                rel.native_intersection(Body(), Body(), 0)

    def test_intersection_cannot_exceed_smaller_body(self):
        with patch.object(rel, '_operation', return_value=[120.]):
            with self.assertRaises(ProgramError):
                rel.native_intersection(Body(), Body(), 0)

    def test_distance_rejects_no_solution_nonfinite_or_inconsistent_points(self):
        for answer in [(-1., None, None), (float('nan'), [], []), (1., (0,0,0), (0,0,0)), (0., None, None)]:
            with self.assertRaises(ProgramError):
                rel._distance(SimpleNamespace(ClosestDistance=lambda a,b: answer), None, None)

    def test_query_validation(self):
        good = {'id': 'gap', 'a': {'body': 'A'}, 'b': {'component': 'B'}}
        self.assertEqual(rel.validate_pairs([good]), [good])
        for bad in [{**good, 'extra': 1}, {**good, 'min_clearance_mm': float('nan')},
                    {**good, 'min_clearance_mm': True}, {**good, 'min_clearance_mm': 2, 'max_clearance_mm': 1},
                    {**good, 'a': {'body': 'A', 'component': 'B'}}, {**good, 'a': {}}]:
            with self.assertRaises(ProgramError): rel.validate_pairs([bad])
        with self.assertRaises(ProgramError): rel.validate_pairs([good, good])

    def test_consumed_multibody_and_alias_scope_rejected_before_measurement(self):
        body = Body(); body.Name = 'A'
        automation = SimpleNamespace(_get_solid_bodies=lambda doc: [body])
        q = {'id': 'pair', 'a': {'body': 'A'}, 'b': {'component': 'C'}}
        for item in [{'bodies': ['A'], 'consumed_by': 'Combined'}, {'bodies': ['A', 'B']}, {'bodies': ['A']}]:
            with patch.object(rel, 'measure_pair') as measure:
                with self.assertRaises(ProgramError):
                    rel.measure_pairs(automation, None, {'native': {'components': {'C': item}}}, [q])
                measure.assert_not_called()


if __name__ == '__main__':
    unittest.main()
