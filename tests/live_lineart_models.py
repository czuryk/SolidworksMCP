"""Opt-in CUDA inference smoke test using verified, separately installed weights."""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def acceptance(model_dir):
    os.environ['SOLIDWORKS_MCP_LINEART_MODELS_DIR'] = str(model_dir.resolve())
    import cv2
    import numpy as np
    from solidworks_mcp.automation import lineart_vectorization as lineart

    bundle = lineart._load_models()
    image = np.full((128, 128, 3), 255, dtype=np.uint8)
    cv2.rectangle(image, (20, 20), (108, 108), (0, 0, 0), 3)
    outputs = {}
    for name in lineart.MODEL_SPECS:
        probability = lineart._infer(image, name, bundle)
        assert probability.shape == (128, 128)
        assert np.isfinite(probability).all()
        assert 0 <= probability.min() < probability.max() <= 1
        outputs[name] = {'shape': list(probability.shape),
                         'min': float(probability.min()), 'max': float(probability.max())}
    return {'status': 'PASS', 'capabilities': bundle['capability'], 'inference': outputs,
            'scope': 'Model integrity, CUDA loading and finite edge probabilities; no CAD tracing commit.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        parser.error('Use a new report path.')
    result = acceptance(args.model_dir)
    args.report.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(result['status'])
