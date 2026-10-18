"""Install verified optional line-art weights from a separately downloaded folder."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp.model_weights import (
    MODEL_SPECS, ModelWeightsError, inspect_lineart_weights,
    model_directory, require_lineart_weights)


def install_from(source, destination=None):
    source = Path(source).resolve()
    if (source / 'lineart').is_dir():
        source = source / 'lineart'
    require_lineart_weights(inspect_lineart_weights(source))
    target = Path(destination).resolve() if destination is not None else model_directory()
    if source == target:
        return inspect_lineart_weights(target)
    target.mkdir(parents=True, exist_ok=True)
    staged = []
    try:
        # Verify the complete source before touching any installed model.
        for spec in MODEL_SPECS.values():
            with tempfile.NamedTemporaryFile(dir=target, suffix='.tmp', delete=False) as handle:
                path = Path(handle.name)
            staged.append((path, target / spec['filename']))
            shutil.copyfile(source / spec['filename'], path)
            digest = hashlib.sha256()
            with path.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(chunk)
            if digest.hexdigest().upper() != spec['sha256']:
                raise ValueError('Source changed during copying: ' + spec['filename'])
        for path, installed in staged:
            os.replace(path, installed)
    finally:
        for path, _ in staged:
            path.unlink(missing_ok=True)
    return require_lineart_weights(inspect_lineart_weights(target))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-dir', type=Path, required=True,
                        help='Models repository root or its lineart folder; no network access is performed')
    parser.add_argument('--destination', type=Path,
                        help='Defaults to the server model directory or SOLIDWORKS_MCP_LINEART_MODELS_DIR')
    args = parser.parse_args()
    try:
        report = install_from(args.from_dir, args.destination)
    except ModelWeightsError as exc:
        print(json.dumps({'success': False, 'code': exc.code, 'message': str(exc),
                          'details': exc.details, 'recommended_actions': exc.actions}, indent=2))
        return 2
    except (OSError, ValueError) as exc:
        print(json.dumps({'success': False, 'code': 'MODEL_INSTALL_FAILED', 'message': str(exc)}))
        return 2
    print(json.dumps({'success': True, **report}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
