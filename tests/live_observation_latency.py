"""Opt-in public MCP latency/actual ImageContent regression on a saved native part."""
import argparse
import asyncio
import base64
import hashlib
import json
import math
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def summary(samples):
    values = sorted(samples)
    return {'count': len(values), 'min_sec': values[0], 'median_sec': statistics.median(values),
            'p95_nearest_rank_sec': values[math.ceil(.95*len(values))-1], 'max_sec': values[-1]}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--document', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--samples', type=int, default=20)
    args = parser.parse_args()
    assert args.samples >= 20, 'At least 20 samples for nearest-rank p95'
    args.output.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    source_sha = hashlib.sha256(args.document.read_bytes()).hexdigest()
    report = {'status': 'IN_PROGRESS', 'document': str(args.document), 'source_sha256': source_sha,
              'samples': [], 'method': 'One cold observation, then interleaved context + three-view observation. Public MCP wall time includes ImageContent delivery; no image reuse.'}
    def save():
        (args.output/'latency.json').write_text(json.dumps(report, indent=2))
    params = StdioServerParameters(command=sys.executable,
        args=['-X', 'utf8', str(root/'solidworks_mcp_server.py')], cwd=str(root))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as client:
            report['version'] = (await client.initialize()).serverInfo.version
            for index in range(args.samples+1):
                started = time.perf_counter()
                raw = await client.call_tool('get_model_context', {})
                elapsed_context = time.perf_counter()-started
                ctx = raw.structuredContent
                assert ctx['success'], ctx
                data = ctx['data']
                assert Path(data['document']['path']) == args.document and not data['document']['dirty']
                with tempfile.TemporaryDirectory(prefix='frames-', dir=args.output) as scratch:
                    started = time.perf_counter()
                    raw = await client.call_tool('observe_model', {'document_path': str(args.document),
                        'expected_revision': data['revision'], 'directory': scratch})
                    elapsed_observe = time.perf_counter()-started
                    result = raw.structuredContent
                    assert result['success'], result
                    observation = result['data']
                    images = [item for item in raw.content if item.type == 'image']
                    assert len(images) == len(observation['images']) == 3
                    assert observation['revision'] == data['revision'] and observation['camera_restored']
                    hashes = []
                    for item, meta in zip(images, observation['images']):
                        digest = hashlib.sha256(base64.b64decode(item.data)).hexdigest()
                        assert digest == meta['delivered_sha256']
                        assert hashlib.sha256(Path(meta['path']).read_bytes()).hexdigest() == meta['sha256']
                        hashes.append(digest)
                        if index in (0, args.samples):
                            dest = args.output/('cold_views' if index == 0 else 'warm_views')
                            dest.mkdir(exist_ok=True)
                            shutil.copyfile(meta['path'], dest/(meta['view']+'.png'))
                    assert len(set(hashes)) == 3, 'Different projections must produce different frames on this fixture'
                sample = {'index': index, 'context_sec': elapsed_context, 'observation_sec': elapsed_observe,
                          'timings_sec': observation['timings_sec'], 'image_hashes': hashes,
                          'image_sizes': [x['delivered_size_px'] for x in observation['images']]}
                if index == 0:
                    report['cold'] = sample
                else:
                    report['samples'].append(sample)
                save()
                if index % 5 == 0: print(index, round(elapsed_context, 3), round(elapsed_observe, 3), flush=True)
            status = (await client.call_tool('get_executor_status', {})).structuredContent['data']
            assert not status['worker_busy'] and not status['quarantined']
            report['executor'] = status
    assert hashlib.sha256(args.document.read_bytes()).hexdigest() == source_sha
    for key in ('context', 'observation'):
        report[key] = summary([x[key+'_sec'] for x in report['samples']])
    report['targets'] = {'context_p95_le_2s': report['context']['p95_nearest_rank_sec'] <= 2,
                         'observation_p95_le_5s': report['observation']['p95_nearest_rank_sec'] <= 5}
    report['status'] = 'MEASURED_SOURCE_PRESERVED'
    save()
    print(json.dumps({k: report[k] for k in ('status', 'context', 'observation', 'targets')}), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
