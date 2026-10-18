"""Opt-in fresh MCP acceptance with an explicitly empty optional-model directory."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = 'Height = model.parameter("Height", 30)\nmodel.revolve("Pedestal", [(0, 10), (Height / 4, 10), (Height, 4)])'


async def acceptance(output):
    output.mkdir(parents=True, exist_ok=False)
    empty = output / 'empty-models'
    empty.mkdir()
    report = {'status': 'IN_PROGRESS', 'calls': [], 'weights_directory': str(empty)}
    def save():
        (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    params = StdioServerParameters(command=sys.executable,
        args=['-X', 'utf8', str(ROOT / 'solidworks_mcp_server.py')], cwd=str(ROOT),
        env={**os.environ, 'SOLIDWORKS_MCP_LINEART_MODELS_DIR': str(empty),
             'HF_HUB_CACHE': str(output / 'empty-region-cache')})
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                report['server_version'] = (await client.initialize()).serverInfo.version
                report['tool_count'] = len((await client.list_tools()).tools)
                assert report['tool_count'] == 13
                async def call(name, arguments, expected=True):
                    raw = await client.call_tool(name, arguments)
                    result = raw.structuredContent
                    assert result is not None, raw
                    while result.get('data', {}).get('status') == 'RUNNING':
                        raw = await client.call_tool('wait_operation', {
                            'operation_id': result['data']['operation_id'], 'wait_sec': 30})
                        result = raw.structuredContent
                    report['calls'].append({'tool': arguments.get('name', name), 'result': result})
                    save()
                    assert result['success'] is expected and bool(raw.isError) is not expected, result
                    print(arguments.get('name', name), 'expected result', expected, flush=True)
                    return result.get('data', {})
                async def expert(name, arguments=None, expected=True):
                    return await call('expert_tools', {'action': 'call', 'name': name,
                                                       'arguments': arguments or {}}, expected)
                await call('get_executor_status', {})
                caps = await expert('get_capabilities')
                lineart = caps['deep_vectorization']['line_art']
                assert not lineart['available'] and len(lineart['missing_files']) == 2
                assert not caps['deep_vectorization']['available']
                assert len(caps['deep_vectorization']['missing_models']) == 2
                report['missing_capability'] = lineart
                arguments = {'image_path': str(output / 'not-needed.png'), 'sketch_name': 'NoWeights',
                             'calibration': {'mode': 'bbox_width', 'value': 100},
                             'trace': {'mode': 'stroke_centerlines'}, 'commit': {'mode': 'force_commit'}}
                missing = await expert('image_to_sketch', arguments, expected=False)
                assert missing['error']['code'] == 'MODEL_WEIGHTS_MISSING'
                assert missing['error']['details']['geometry_created'] is False
                region = await expert('image_to_sketch', {
                    **arguments, 'trace': {'mode': 'outer_silhouette'}}, expected=False)
                assert region['error']['code'] == 'MODEL_WEIGHTS_MISSING'
                assert region['error']['details']['backend'] == 'sam2_vitmatte'
                await call('model_api_lookup', {'topic': 'index'})
                await call('execute_model_code', {'mode': 'analyze', 'code': PROGRAM})
                document = output / (output.name + '_Pedestal.SLDPRT')
                await call('execute_model_code', {'mode': 'create', 'code': PROGRAM,
                           'output_path': str(document), 'observe': False})
                context = await call('get_model_context', {})
                facts = await call('inspect_model', {'document_path': str(document),
                    'expected_revision': context['revision'], 'precision': 'delivery'})
                report['native_inspection'] = facts
                assert len(facts['bodies']) == 1, facts
                body = facts['bodies'][0]
                assert body['kernel_health']['passed'] is True, body
                assert body['dimensions_mm'] == [20, 30, 20], body
                assert abs(body['volume_exact'] - 6031.857895) < 0.001, body
                assert document.is_file() and document.stat().st_size > 0
                # A failed optional request must leave the saved native model usable.
                again = await expert('image_to_sketch', arguments, expected=False)
                assert again['error']['code'] == 'MODEL_WEIGHTS_MISSING'
                after = await call('get_model_context', {})
                assert after['revision'] == context['revision']
                await expert('close_document', {'save': True})
                assert list(empty.iterdir()) == [], 'The server must not download weights implicitly'
                assert not (output / 'empty-region-cache').exists()
                report['status'] = 'PASS'
    except BaseException as exc:
        report.update(status='FAILED', error=repr(exc))
        raise
    finally:
        save()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    result = asyncio.run(acceptance(args.output_dir.resolve()))
    print(result['status'])
