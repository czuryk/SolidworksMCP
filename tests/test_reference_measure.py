"""Reference estimates remain bounded, explicit about ambiguity and COM-free."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw
from solidworks_mcp.reference_measure import measure_reference
from solidworks_mcp.shape_program import ProgramError


class ReferenceMeasureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'reference.png'
        image = Image.new('RGB', (200, 240), 'white')
        ImageDraw.Draw(image).rectangle((40, 20, 159, 219), fill='gray')
        image.save(self.path)

    def query(self, kind='width_at_height', **extra):
        q = {'id': 'body', 'region': [10, 10, 190, 230], 'kind': kind}
        if kind in {'width_at_height', 'depth_range'}: q['y'] = 120
        return {**q, **extra}

    def test_metric_width_and_uncertainty(self):
        result = measure_reference(str(self.path), [self.query()], projection='front',
                                   calibration={'pixel_distance': 200, 'length_mm': 100})
        m = result['measurements'][0]
        self.assertEqual(m['value'], 120)
        self.assertEqual(m['metric']['value'], 60)
        self.assertLess(m['metric']['interval'][0], 60)
        self.assertGreater(m['metric']['interval'][1], 60)
        self.assertEqual(result['cad_entities_created'], 0)

    def test_batch_bbox_center_ratio_and_crop(self):
        queries = [self.query(kind, id=kind) for kind in ('bbox', 'center', 'aspect_ratio')]
        result = measure_reference(str(self.path), queries, annotation_directory=self.temp.name)
        self.assertEqual([m['value'] for m in result['measurements']], [[40,20,160,220],[100,120],.6])
        self.assertEqual(len(result['images']), 3)
        self.assertTrue(all(Path(i['path']).is_file() for i in result['images']))

    def test_hidden_depth_is_not_invented(self):
        for projection, visibility in [('front','visible'), ('right','occluded'), ('right','unknown')]:
            result = measure_reference(str(self.path), [self.query('depth_range',visibility=visibility)], projection=projection)
            self.assertEqual(result['measurements'][0]['status'], 'UNKNOWN')
            self.assertIsNone(result['measurements'][0]['value'])
        self.assertEqual(measure_reference(str(self.path), [self.query('depth_range')],projection='right')['measurements'][0]['value'],120)

    def test_cache_is_content_addressed(self):
        a = measure_reference(str(self.path), [self.query()])
        b = measure_reference(str(self.path), [self.query()])
        self.assertTrue(b['cache_hit'])
        Image.new('RGB', (200,240), 'white').save(self.path)
        c = measure_reference(str(self.path), [self.query()])
        self.assertNotEqual(a['reference']['sha256'], c['reference']['sha256'])
        self.assertEqual(c['measurements'][0]['status'], 'UNKNOWN')

    def test_clipped_roi_lowers_confidence(self):
        result = measure_reference(str(self.path), [self.query(region=[50,50,100,150])])
        self.assertIn('FOREGROUND_TOUCHES_ROI_BOUNDARY',result['measurements'][0]['warnings'])
        self.assertEqual(result['measurements'][0]['confidence'],'low')

    def test_invalid_input_rejected_before_artifact(self):
        cases = [[self.query(y=120.5)], [self.query(region=[-1,0,200,240])],
                 [self.query(),self.query()], [self.query(unknown_key=True)],
                 [self.query('bbox',y=120)], [self.query()]*13]
        for queries in cases:
            with self.assertRaises(ProgramError): measure_reference(str(self.path), queries)
        with self.assertRaises(ProgramError):
            measure_reference(str(self.path),[self.query()],projection='perspective',
                              calibration={'pixel_distance':100,'length_mm':10})

    def test_reference_tool_is_com_free_even_when_writer_is_busy(self):
        from solidworks_mcp.server import call_tool
        from solidworks_mcp.automation.session_lease import SessionLease
        with SessionLease(Path(self.temp.name)/'test-writer.lock'), patch('solidworks_mcp.server.sw_automation._document_snapshot',
                                   side_effect=AssertionError('COM accessed')):
            result = asyncio.run(call_tool('measure_reference', {'image_path':str(self.path),'queries':[self.query()],
                                          'annotation_directory':self.temp.name}))
        self.assertFalse(result.isError)
        self.assertTrue(any(c.type=='image' for c in result.content))


class ExpectedSizeChecks(unittest.TestCase):
    """Numeric comparison of a reference element with the model."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'approved_front.png'
        image = Image.new('RGB', (400, 300), 'white')
        # A 120 x 60 px rectangle; at 0.25 mm/px that is 30 x 15 mm.
        ImageDraw.Draw(image).rectangle((100, 80, 219, 139), fill='black')
        image.save(self.path)
        self.calibration = {'pixel_distance': 400, 'length_mm': 100}

    def tearDown(self):
        self.temp.cleanup()

    def check(self, expected, tolerance=0.5, calibration='default', **query):
        calibration = self.calibration if calibration == 'default' else calibration
        item = {'id': 'visor', 'kind': 'bbox', 'region': [50, 40, 300, 200], 'expected_mm': expected, **query}
        if tolerance is not None:
            item['tolerance_mm'] = tolerance
        data = measure_reference(str(self.path), [item], 'front' if calibration else 'unknown', calibration)
        return data['measurements'][0]['check'], data

    def test_correct_size_passes(self):
        check, data = self.check({'width': 30, 'height': 15})
        self.assertIs(check['passed'], True)
        self.assertEqual(check['delta_mm'], {'width': 0.0, 'height': 0.0})
        self.assertEqual(check['measured_mm'], {'width': 30.0, 'height': 15.0})
        self.assertEqual(data['checks'], {'total': 1, 'passed': 1, 'failed': 0, 'unknown': 0})

    def test_deviation_beyond_tolerance_fails_with_signed_delta(self):
        check, data = self.check({'width': 30, 'height': 13.4}, tolerance=1.5)
        self.assertIs(check['passed'], False)
        self.assertEqual(check['delta_mm']['height'], 1.6)
        self.assertEqual(data['checks']['failed'], 1)
        self.assertIs(self.check({'width': 30, 'height': 13.4}, tolerance=2)[0]['passed'], True)

    def test_occluded_or_unmeasured_is_unknown_not_passed(self):
        check, data = self.check({'width': 30}, visibility='occluded')
        self.assertIsNone(check['passed'])
        self.assertIsNone(check['delta_mm'])
        self.assertEqual(check['reason'], 'OCCLUDED_OR_UNKNOWN_BOUNDARY')
        self.assertEqual(data['checks']['unknown'], 1)
        empty, _ = self.check({'width': 30}, region=[300, 200, 390, 290])
        self.assertIsNone(empty['passed'])
        self.assertEqual(empty['reason'], 'NO_STABLE_FOREGROUND')

    def test_missing_scale_tolerance_or_clipped_roi_is_unknown(self):
        self.assertEqual(self.check({'width': 30}, calibration=None)[0]['reason'], 'CALIBRATION_REQUIRED')
        untolerated, _ = self.check({'width': 31}, tolerance=None)
        self.assertIsNone(untolerated['passed'])
        self.assertEqual(untolerated['delta_mm'], {'width': -1.0})
        clipped, _ = self.check({'width': 30}, region=[150, 40, 300, 200])
        self.assertIsNone(clipped['passed'])
        self.assertEqual(clipped['reason'], 'FOREGROUND_TOUCHES_ROI_BOUNDARY')

    def test_centers_use_the_model_origin_when_given(self):
        image_frame, _ = self.check({'center_x': 40, 'center_y': 27.5})
        self.assertIs(image_frame['passed'], True)
        self.assertEqual(image_frame['center_frame'], 'image_top_left_y_down')
        # Model origin at pixel (200, 300): the centre is 10 mm left of it and 47.5 mm above.
        calibrated = {**self.calibration, 'origin_px': [200, 300]}
        model_frame, _ = self.check({'center_x': -10, 'center_y': 47.5}, calibration=calibrated)
        self.assertIs(model_frame['passed'], True)
        self.assertEqual(model_frame['center_frame'], 'model_origin_y_up')

    def test_row_width_and_batch_of_twelve(self):
        row = {'id': 'row', 'kind': 'width_at_height', 'region': [50, 40, 300, 200], 'y': 100,
               'expected_mm': {'width': 30}, 'tolerance_mm': 0.5}
        batch = [{**row, 'id': f'row{i}'} for i in range(12)]
        data = measure_reference(str(self.path), batch, 'front', self.calibration)
        self.assertEqual(data['checks'], {'total': 12, 'passed': 12, 'failed': 0, 'unknown': 0})
        self.assertTrue(all('delta_mm' in m['check'] for m in data['measurements']))

    def test_invalid_expectations_are_rejected(self):
        for query in ({'expected_mm': {'depth': 1}}, {'expected_mm': {}}, {'expected_mm': {'width': 'x'}},
                      {'tolerance_mm': 1}, {'expected_mm': {'width': 1}, 'tolerance_mm': 0},
                      {'kind': 'aspect_ratio', 'expected_mm': {'width': 1}},
                      {'kind': 'center', 'expected_mm': {'width': 1}}):
            item = {'id': 'a', 'kind': 'bbox', 'region': [50, 40, 300, 200], **query}
            with self.assertRaises(ProgramError):
                measure_reference(str(self.path), [item], 'front', self.calibration)

    def test_plain_queries_are_unchanged(self):
        data = measure_reference(str(self.path), [{'id': 'a', 'kind': 'bbox', 'region': [50, 40, 300, 200]}])
        self.assertNotIn('check', data['measurements'][0])
        self.assertNotIn('checks', data)


if __name__ == '__main__': unittest.main()
