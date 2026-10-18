"""Missing optional models must not block startup, CAD or unrelated image tools."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from solidworks_mcp import model_weights as weights
from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.automation import deep_vectorization, lineart_vectorization
from scripts.install_vectorization_models import install_from


class OptionalModelWeightsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'lineart'
        self.directory.mkdir()
        self.env = patch.dict(os.environ, {weights.DIRECTORY_ENV: str(self.directory)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.content = {name: ('test-weights-' + name).encode() for name in weights.MODEL_SPECS}
        specs = {name: {**spec, 'sha256': hashlib.sha256(self.content[name]).hexdigest().upper()}
                 for name, spec in weights.MODEL_SPECS.items()}
        self.specs = patch.dict(weights.MODEL_SPECS, specs)
        self.specs.start()
        self.addCleanup(self.specs.stop)
        weights._file_identity.cache_clear()
        self.addCleanup(weights._file_identity.cache_clear)

    def write_models(self):
        for name, spec in weights.MODEL_SPECS.items():
            (self.directory / spec['filename']).write_bytes(self.content[name])

    def test_missing_files_report_filenames_and_install_location(self):
        report = weights.inspect_lineart_weights()
        self.assertEqual(report['weights_status'], 'missing')
        self.assertEqual(len(report['missing_files']), 2)
        with self.assertRaises(weights.ModelWeightsError) as caught:
            weights.require_lineart_weights(report)
        self.assertEqual(caught.exception.code, 'MODEL_WEIGHTS_MISSING')
        self.assertIn(str(self.directory), str(caught.exception))
        self.assertIn('SolidworksMCP-models', json.dumps(caught.exception.details))

    def test_missing_one_model_names_only_that_file(self):
        self.write_models()
        (self.directory / weights.MODEL_SPECS['teed']['filename']).unlink()
        self.assertEqual(weights.inspect_lineart_weights()['missing_files'], ['teed_biped_5.pt'])

    def test_valid_weights_are_ready(self):
        self.write_models()
        self.assertEqual(weights.require_lineart_weights()['weights_status'], 'ready')

    def test_lfs_pointer_is_invalid_with_specific_recovery(self):
        self.write_models()
        (self.directory / 'dexined_biped_v2.pt').write_text(
            'version https://git-lfs.github.com/spec/v1\noid sha256:fake\nsize 123\n')
        report = weights.inspect_lineart_weights()
        self.assertEqual(report['models']['dexined']['status'], 'lfs_pointer')
        with self.assertRaises(weights.ModelWeightsError) as caught:
            weights.require_lineart_weights(report)
        self.assertEqual(caught.exception.code, 'MODEL_WEIGHTS_INVALID')
        self.assertIn('git lfs pull', ' '.join(caught.exception.actions))

    def test_corruption_is_distinct_from_a_missing_file(self):
        self.write_models()
        (self.directory / 'teed_biped_5.pt').write_bytes(b'damaged')
        report = weights.inspect_lineart_weights()
        self.assertEqual(report['invalid_files'], ['teed_biped_5.pt'])
        self.assertEqual(report['models']['teed']['status'], 'checksum_mismatch')

    def test_unreadable_file_does_not_break_capability_reporting(self):
        self.write_models()
        with patch.object(weights, '_file_identity', side_effect=PermissionError('denied')):
            report = weights.inspect_lineart_weights()
        self.assertEqual(report['weights_status'], 'invalid')
        self.assertEqual(report['models']['dexined']['status'], 'unreadable')

    def test_installing_files_after_missing_probe_becomes_ready(self):
        self.assertEqual(weights.inspect_lineart_weights()['weights_status'], 'missing')
        self.write_models()
        self.assertEqual(weights.inspect_lineart_weights()['weights_status'], 'ready')

    def test_installer_accepts_models_repository_root(self):
        self.write_models()
        destination = self.root / 'installed'
        report = install_from(self.root, destination)
        self.assertEqual(report['weights_status'], 'ready')
        for name, spec in weights.MODEL_SPECS.items():
            self.assertEqual((destination / spec['filename']).read_bytes(), self.content[name])
        self.assertFalse(list(destination.glob('*.tmp')))

    def test_invalid_source_does_not_replace_existing_installation(self):
        self.write_models()
        destination = self.root / 'installed'
        install_from(self.root, destination)
        (self.directory / 'teed_biped_5.pt').write_bytes(b'bad')
        with self.assertRaises(weights.ModelWeightsError):
            install_from(self.root, destination)
        self.assertEqual((destination / 'teed_biped_5.pt').read_bytes(), self.content['teed'])

    def test_capabilities_distinguish_missing_weights_from_cuda(self):
        with patch.object(lineart_vectorization, '_lightweight_cuda_probe', return_value={
                'torch': False, 'cuda': False, 'device': None, 'torch_cuda_build': None,
                'probe': 'test'}):
            report = lineart_vectorization.capability_report()
        self.assertFalse(report['available'])
        self.assertEqual(report['weights_status'], 'missing')
        self.assertEqual(len(report['missing_files']), 2)

    def test_lineart_loader_reports_missing_weights_before_torch_loading(self):
        with patch.object(lineart_vectorization, '_MODEL_BUNDLE', None):
            with self.assertRaises(weights.ModelWeightsError) as caught:
                lineart_vectorization._load_models()
        self.assertEqual(caught.exception.code, 'MODEL_WEIGHTS_MISSING')

    def test_region_loader_identifies_its_own_missing_models(self):
        with patch.object(deep_vectorization, '_MODEL_BUNDLE', None), patch.object(
                deep_vectorization, '_hf_repo_cached', return_value=False):
            with self.assertRaises(weights.ModelWeightsError) as caught:
                deep_vectorization._load_models('test/sam', 'test/matte')
        self.assertEqual(caught.exception.details['missing_models'], ['test/sam', 'test/matte'])
        self.assertEqual(caught.exception.details['backend'], 'sam2_vitmatte')

    def test_region_and_classical_modes_do_not_require_lineart_weights(self):
        with patch.object(deep_vectorization, 'require_region_weights'), patch.object(
                weights, 'require_lineart_weights', side_effect=AssertionError('Not a line-art request')):
            for arguments in ({}, {'trace': {'mode': 'outer_silhouette', 'backend': 'classical'}},
                              {'trace': {'mode': 'outer_silhouette', 'backend': 'sam2_vitmatte'}}):
                weights.preflight_vectorization_request(arguments)

    def test_region_preflight_uses_requested_model_ids(self):
        with patch.object(deep_vectorization, '_hf_repo_cached', return_value=False):
            with self.assertRaises(weights.ModelWeightsError) as caught:
                weights.preflight_vectorization_request({'models': {
                    'sam_model_id': 'test/custom-sam', 'matte_model_id': 'test/custom-matte'}})
        self.assertEqual(caught.exception.details['missing_models'],
                         ['test/custom-sam', 'test/custom-matte'])

    def test_direct_vectorization_returns_structured_error_without_cad_commit(self):
        from PIL import Image, ImageDraw
        image = Image.new('RGB', (128, 128), 'white')
        ImageDraw.Draw(image).line((10, 64, 118, 64), fill='black', width=3)
        path = self.root / 'drawing.png'
        image.save(path)
        automation = SolidWorksAutomation()
        automation.create_parametric_sketch = Mock(side_effect=AssertionError('CAD must not run'))
        with patch.object(lineart_vectorization, '_MODEL_BUNDLE', None):
            result = automation.image_to_sketch(str(path), 'MissingWeights',
                trace={'mode': 'stroke_centerlines'}, calibration={'mode': 'bbox_width', 'value': 100})
        self.assertFalse(result['success'])
        self.assertEqual(result['data']['error']['code'], 'MODEL_WEIGHTS_MISSING')
        self.assertTrue(result['data']['error']['recoverable'])
        automation.create_parametric_sketch.assert_not_called()

    def test_mcp_mutation_rejects_missing_weights_before_queueing_cad(self):
        from solidworks_mcp import server
        with patch.object(server.cad_executor, 'submit', side_effect=AssertionError('No CAD queue')):
            raw = asyncio.run(server._dispatch_tool('expert_tools', {
                'action': 'call', 'name': 'image_to_sketch', 'arguments': {
                    'image_path': str(self.root / 'drawing.png'), 'sketch_name': 'MissingWeights',
                    'calibration': {'mode': 'bbox_width', 'value': 100},
                    'trace': {'mode': 'stroke_centerlines'}, 'commit': {'mode': 'force_commit'}}}))
        self.assertTrue(raw.isError)
        self.assertEqual(raw.structuredContent['data']['error']['code'], 'MODEL_WEIGHTS_MISSING')

    def test_mcp_default_region_mode_rejects_missing_cache_before_cad(self):
        from solidworks_mcp import server
        with patch.object(server.cad_executor, 'submit', side_effect=AssertionError('No CAD queue')), \
                patch.object(deep_vectorization, '_hf_repo_cached', return_value=False):
            raw = asyncio.run(server._dispatch_tool('expert_tools', {
                'action': 'call', 'name': 'image_to_sketch', 'arguments': {
                    'image_path': str(self.root / 'drawing.png'), 'sketch_name': 'MissingRegions',
                    'calibration': {'mode': 'bbox_width', 'value': 100},
                    'commit': {'mode': 'force_commit'}}}))
        error = raw.structuredContent['data']['error']
        self.assertTrue(raw.isError)
        self.assertEqual(error['code'], 'MODEL_WEIGHTS_MISSING')
        self.assertEqual(error['details']['backend'], 'sam2_vitmatte')


if __name__ == '__main__':
    unittest.main()
