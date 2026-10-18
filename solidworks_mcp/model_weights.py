"""Optional vectorization weights: offline discovery and integrity diagnostics."""

import hashlib
import os
from functools import lru_cache
from pathlib import Path


MODELS_REPOSITORY = 'https://github.com/czuryk/SolidworksMCP-models'
INSTALLATION_URL = 'https://github.com/czuryk/SolidworksMCP/blob/main/docs/setup.md#optional-vectorization-models'
DIRECTORY_ENV = 'SOLIDWORKS_MCP_LINEART_MODELS_DIR'
LINE_ART_MODES = {'stroke_centerlines', 'stroke_edges', 'all_visible_edges'}
MODEL_SPECS = {
    'dexined': {
        'filename': 'dexined_biped_v2.pt',
        'sha256': '096C2230D3060104BAE083A4B0C882EBC6301D730C401C7E8556A6D8F944C65B',
        'mean_bgr': [103.939, 116.779, 123.68],
        'multiple': 16,
        'source': 'xavysp/DexiNed official BIPED v2 checkpoint',
    },
    'teed': {
        'filename': 'teed_biped_5.pt',
        'sha256': '3175EC531B2238EAAACBCFAC17111666F2B8C885BD04F785DE36F8C776C80C3A',
        'mean_bgr': [104.007, 116.669, 122.679],
        'multiple': 8,
        'source': 'xavysp/TEED official BIPED checkpoint 5',
    },
}


class ModelWeightsError(RuntimeError):
    """A recoverable installation problem, not a failed image or CAD operation."""

    def __init__(self, code, message, details, actions):
        super().__init__(message)
        self.code = code
        self.details = details
        self.actions = actions


def model_directory():
    configured = os.environ.get(DIRECTORY_ENV)
    return (Path(configured).expanduser().resolve() if configured else
            Path(__file__).resolve().parent / 'models' / 'lineart')


@lru_cache(maxsize=16)
def _file_identity(path_text, size, modified_ns):
    digest = hashlib.sha256()
    with Path(path_text).open('rb') as handle:
        prefix = handle.read(128)
        if prefix.startswith(b'version https://git-lfs.github.com/spec/v1'):
            return 'lfs_pointer', None
        digest.update(prefix)
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return 'binary', digest.hexdigest().upper()


def inspect_lineart_weights(directory=None):
    directory = Path(directory).resolve() if directory is not None else model_directory()
    files = {}
    for name, spec in MODEL_SPECS.items():
        path = directory / spec['filename']
        item = {'path': str(path), 'filename': spec['filename'], 'exists': False,
                'sha256_verified': False, 'expected_sha256': spec['sha256'],
                'source': spec['source'], 'status': 'missing'}
        try:
            stat = path.stat()
            item['exists'] = True
            kind, digest = _file_identity(str(path), stat.st_size, stat.st_mtime_ns)
            verified = digest == spec['sha256']
            item.update(sha256_verified=verified, actual_sha256=digest,
                        status=('ready' if verified else
                                'lfs_pointer' if kind == 'lfs_pointer' else 'checksum_mismatch'))
        except FileNotFoundError:
            pass
        except OSError as exc:
            item.update(status='unreadable', error=str(exc))
        files[name] = item
    missing = [item['filename'] for item in files.values() if item['status'] == 'missing']
    invalid = [item['filename'] for item in files.values()
               if item['status'] not in {'ready', 'missing'}]
    return {'weights_status': 'invalid' if invalid else 'missing' if missing else 'ready',
            'models_directory': str(directory), 'models': files,
            'missing_files': missing, 'invalid_files': invalid,
            'models_repository': MODELS_REPOSITORY, 'installation_url': INSTALLATION_URL}


def require_lineart_weights(report=None):
    report = inspect_lineart_weights() if report is None else report
    if report['weights_status'] == 'ready':
        return report
    invalid = report['invalid_files']
    code = 'MODEL_WEIGHTS_INVALID' if invalid else 'MODEL_WEIGHTS_MISSING'
    affected = report['missing_files'] + invalid
    message = ('Line-art vectorization requires the verified DexiNed and TEED weight files. '
               + ('Missing files: ' if not invalid else 'Missing or invalid files: ')
               + ', '.join(affected) + '. Install them in ' + report['models_directory']
               + '. See ' + INSTALLATION_URL + '. Other modeling tools remain available.')
    actions = [f'Download the optional weights from {MODELS_REPOSITORY}.',
               'Run python scripts/install_vectorization_models.py --from-dir <models-repository-folder>.',
               f'Alternatively set {DIRECTORY_ENV} to the folder containing both verified .pt files.',
               'Call expert_tools with action=call and name=get_capabilities to check readiness.']
    if any(item['status'] == 'lfs_pointer' for item in report['models'].values()):
        actions.insert(0, 'The supplied files are Git LFS pointers, not model weights. '
                       'Run git lfs pull in the models repository when its LFS quota permits downloading.')
    raise ModelWeightsError(code, message, {**report, 'backend': 'line_art',
                                           'geometry_created': False}, actions)


def preflight_vectorization_request(arguments):
    """Reject missing weights before a public vectorization call enters CAD."""
    trace = arguments.get('trace') or {}
    if trace.get('mode') in LINE_ART_MODES:
        if trace.get('backend') in {None, 'line_art', 'deep_line_art', 'dexined_teed'}:
            require_lineart_weights()
    elif trace.get('backend', 'deep_matting') in {'deep', 'deep_matting', 'sam2_vitmatte'}:
        from .automation.deep_vectorization import (
            MATTE_MODEL_ID, SAM_MODEL_ID, require_region_weights)
        models = arguments.get('models') or {}
        require_region_weights(models.get('sam_model_id', SAM_MODEL_ID),
                               models.get('matte_model_id', MATTE_MODEL_ID))
