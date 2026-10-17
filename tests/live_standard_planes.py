"""Opt-in issue #1 acceptance through a fresh MCP process and live SolidWorks.

Creates six new parts in a new directory; never changes the installed service pack.
Polish names simulate localized/renamed planes on the installed SolidWorks locale.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
NAMES = {
    'english': ['Front Plane', 'Top Plane', 'Right Plane'],
    'polish': ['Płaszczyzna przednia', 'Płaszczyzna górna', 'Płaszczyzna prawa'],
}
EXPECTED = {'Front': [100, 50, 20], 'Top': [100, 20, 50], 'Right': [20, 50, 100]}


async def acceptance(output):
    output.mkdir(parents=True, exist_ok=False)
    report = {'status': 'IN_PROGRESS', 'date': datetime.now(timezone.utc).isoformat(),
              'locale_test': 'Renamed native planes; not a Polish UI installation',
              'calls': [], 'cases': [], 'source_sha256': {}}
    for relative in ('solidworks_mcp/automation/com_utils.py',
                     'solidworks_mcp/automation/sketches.py',
                     'solidworks_mcp/automation/shape_native.py',
                     'tests/live_standard_planes.py'):
        report['source_sha256'][relative] = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()

    def save():
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    params = StdioServerParameters(command=sys.executable,
        args=['-X', 'utf8', str(ROOT / 'solidworks_mcp_server.py')], cwd=str(ROOT))
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                report['server_version'] = (await client.initialize()).serverInfo.version

                async def call(name, arguments):
                    raw = await client.call_tool(name, arguments)
                    result = raw.structuredContent
                    while result.get('data', {}).get('status') == 'RUNNING':
                        raw = await client.call_tool('wait_operation', {
                            'operation_id': result['data']['operation_id'], 'wait_sec': 30})
                        result = raw.structuredContent
                    label = arguments.get('name', name)
                    report['calls'].append({'tool': label, 'result': result})
                    save()
                    print(label, result['success'], flush=True)
                    assert result['success'] and not raw.isError, result
                    return result.get('data', {})

                async def expert(name, **arguments):
                    return await call('expert_tools', {'action': 'call', 'name': name,
                                                      'arguments': arguments})

                async def run(code):
                    data = await expert('execute_python', code=code)
                    return json.loads(data['stdout'])

                report['solidworks'] = await expert('get_solidworks_info')
                for locale, names in NAMES.items():
                    for index, plane in enumerate(EXPECTED):
                        path = output / f'issue1_{locale}_{plane}.SLDPRT'
                        await expert('create_new_part', save_path=str(path))
                        await expert('save_document', filepath=str(path))
                        guard = f"assert os.path.normcase(com_get(doc, 'GetPathName')) == os.path.normcase({str(path)!r})\n"
                        renamed = await run(guard +
                            "planes = [f for f in automation._walk_features(doc, include_sub=False)\n"
                            "          if com_get(f, 'GetTypeName2') == 'RefPlane'][:3]\n"
                            f"names = {names!r}\n"
                            "assert len(planes) == 3\n"
                            "for f, name in zip(planes, names):\n"
                            "    f.Name = name\n"
                            "assert [com_get(f, 'Name') for f in planes] == names\n"
                            "print(json.dumps({'names': names}))")
                        sketch = await expert('create_sketch', plane=plane)
                        assert sketch['selection']['resolved_name'] == names[index], sketch
                        assert sketch['orientation']['normal_to_verified'], sketch
                        normal = sketch['orientation']['sketch_axes']['normal']
                        normal_axis = {'Front': 2, 'Top': 1, 'Right': 0}[plane]
                        assert abs(abs(normal[normal_axis]) - 1) < 1e-9, normal
                        await expert('draw_rectangle', x1=0, y1=0, x2=100, y2=50, unit='mm')
                        await expert('close_sketch')
                        await expert('advanced_extrude', depth=20, unit='mm')
                        measure = guard + (
                            "from solidworks_mcp.automation.model_inspection import exact_bbox, kernel_health\n"
                            "bodies = list(com_get(typed(doc, 'IPartDoc'), 'GetBodies2', 0, False) or [])\n"
                            "assert len(bodies) == 1\n"
                            "box = exact_bbox(bodies[0])\n"
                            "size = [b-a for a,b in zip(box['min'], box['max'])]\n"
                            "faces = int(com_get(bodies[0], 'GetFaceCount'))\n"
                            "health = kernel_health(bodies[0])\n"
                            "assert faces == 6 and health['passed']\n"
                            f"assert all(abs(a-b) < 1e-6 for a,b in zip(size, {EXPECTED[plane]!r})), size\n"
                            "print(json.dumps({'body_count': len(bodies), 'face_count': faces,\n"
                            "                  'bbox_mm': box, 'size_mm': size, 'kernel': health}))")
                        before = await run(measure)
                        await expert('save_document')
                        await expert('close_document', save=True)
                        await expert('open_document', filepath=str(path))
                        reopened = await run(measure)
                        assert before == reopened, (before, reopened)
                        step = path.with_suffix('.step')
                        await expert('export_file', filepath=str(step))
                        step_text = step.read_text(encoding='ascii', errors='replace')
                        assert 'ISO-10303-21;' in step_text and 'MANIFOLD_SOLID_BREP' in step_text
                        await expert('close_document', save=True)
                        case = {'locale': locale, 'plane': plane, 'plane_names': renamed['names'],
                                'selection': sketch['selection'], 'sketch_normal': normal,
                                'before_save': before, 'after_reopen': reopened, 'artifacts': {}}
                        for artifact in (path, step):
                            case['artifacts'][artifact.name] = {'bytes': artifact.stat().st_size,
                                'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}
                        report['cases'].append(case)
                        save()
                report['status'] = 'PASS'
    except BaseException as exc:
        report['status'] = 'FAILED'
        report['error'] = repr(exc)
        raise
    finally:
        save()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(acceptance(args.output_dir.resolve()))
    print(json.dumps({'status': result['status'], 'cases': len(result['cases'])}))
