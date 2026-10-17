"""Opt-in MCP error delivery acceptance against an unchanged saved native fixture."""
import argparse
import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(document, output):
    root = Path(__file__).resolve().parents[1]
    report = {'status': 'IN_PROGRESS', 'calls': []}
    original = hashlib.sha256(document.read_bytes()).hexdigest()

    def save():
        output.write_text(json.dumps(report, indent=2), encoding='utf-8')

    params = StdioServerParameters(command=sys.executable,
        args=['-X', 'utf8', str(root / 'solidworks_mcp_server.py')], cwd=str(root))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as client:
            version = (await client.initialize()).serverInfo.version
            assert version == '6.18.0', version
            report['version'] = version

            async def call(name, arguments, success=True):
                raw = await client.call_tool(name, arguments)
                result = raw.structuredContent
                report['calls'].append({'name': name, 'result': result})
                save()
                print(name, result['success'], flush=True)
                assert result['success'] is success, result
                assert raw.isError is not success
                return raw, result['data']

            async def expert(name, arguments, success=True):
                return await call('expert_tools', {'action': 'call', 'name': name,
                    'arguments': arguments}, success)

            await expert('open_document', {'filepath': str(document)})
            _, before = await call('get_model_context', {'include_source': True})
            target = {'document_path': str(document), 'expected_revision': before['revision']}
            _, before_facts = await call('inspect_model', {**target, 'precision': 'delivery'})

            cases = [
                ("import sys\nprint('BEFORE_ASSERT')\nprint('NATIVE_WARNING', file=sys.stderr)\n"
                 "raise AssertionError('intentional diagnostics fixture')",
                 'PYTHON_EXECUTION_ERROR', 'PARTIAL_EFFECTS_POSSIBLE', 'BEFORE_ASSERT\n'),
                ("print('NEVER_EXECUTED')\nif :", 'PYTHON_SYNTAX_ERROR', 'NOT_EXECUTED', ''),
                ("print('BEFORE_SCRIPT_EXIT')\nraise SystemExit(2)",
                 'PYTHON_SCRIPT_INTERRUPTED', 'PARTIAL_EFFECTS_POSSIBLE', 'BEFORE_SCRIPT_EXIT\n'),
                ("from solidworks_mcp.shape_program import ProgramError\nprint('BEFORE_NATIVE_ERROR')\n"
                 "raise ProgramError('FEATURE_DEAD', 'simulated native recipe failure', feature='Fixture')",
                 'FEATURE_DEAD', 'PARTIAL_EFFECTS_POSSIBLE', 'BEFORE_NATIVE_ERROR\n'),
                ("import pywintypes\nprint('SIMULATED_HRESULT')\n"
                 "raise pywintypes.com_error(-2147417848, 'simulated stale proxy', None, None)",
                 'COM_OBJECT_DISCONNECTED', 'PARTIAL_EFFECTS_POSSIBLE', 'SIMULATED_HRESULT\n'),
            ]
            for code, expected, state, stdout in cases:
                raw, data = await expert('execute_python', {'code': code}, False)
                error = data['error']
                assert error['code'] == expected, error
                assert error['details']['script_state'] == state
                assert error['details']['automatic_retry'] is False
                assert error['document_restored'] is None
                assert data['stdout'] == stdout
                assert data['traceback']
                text = '\n'.join(x.text for x in raw.content if x.type == 'text')
                if stdout:
                    assert stdout.strip() in text
                if expected == 'PYTHON_EXECUTION_ERROR':
                    assert data['stderr'] == 'NATIVE_WARNING\n'
                    assert 'NATIVE_WARNING' in text
                if expected == 'FEATURE_DEAD':
                    assert error['details']['native_details']['feature'] == 'Fixture'
                _, context = await call('get_model_context', {})
                assert context['revision'] == before['revision']

            _, queued = await expert('execute_python_async', {
                'code': "print('ASYNC_EVIDENCE')\nraise ValueError('intentional async failure')"})
            # Poll the core session tool before making assertions or closing stdio.
            # A test-client exception must not orphan an active CAD operation.
            polled = await client.call_tool('get_job_result', {'job_id': queued['job_id'], 'timeout': 60})
            polled_result = polled.structuredContent
            report['calls'].append({'name': 'get_job_result', 'result': polled_result})
            save()
            job = polled_result['data']
            assert job['operation']['status'] == 'COMPLETED', job
            assert polled_result['success'] is False
            report['async_job'] = job
            nested = job['operation']['result']
            assert not nested['success']
            assert nested['data']['stdout'] == 'ASYNC_EVIDENCE\n'
            assert nested['data']['error']['code'] == 'PYTHON_EXECUTION_ERROR'

            _, success = await expert('execute_python', {'code': "print('AFTER_FAILURES')\nresult=42"})
            assert success['stdout'] == 'AFTER_FAILURES\n'
            assert success['result'] == '42'
            _, after = await call('get_model_context', {'include_source': True})
            assert before['revision'] == after['revision']
            _, facts = await call('inspect_model', {**target, 'precision': 'delivery'})
            assert facts['bodies'] == before_facts['bodies']
            assert all(b['kernel_health']['passed'] for b in facts['bodies'])
            raw, views = await call('observe_model', {**target, 'directory': str(output.parent / 'release_views')})
            images = [x for x in raw.content if x.type == 'image']
            assert len(images) == len(views['images']) == 3
            for image, meta in zip(images, views['images']):
                assert hashlib.sha256(base64.b64decode(image.data)).hexdigest() == meta['delivered_sha256']
            _, status = await call('get_executor_status', {})
            assert not status['quarantined']
            assert original == hashlib.sha256(document.read_bytes()).hexdigest()
            report.update(status='RAW_DIAGNOSTICS_6_18_PASS', source_sha256=original,
                          native_revision_unchanged=True, body_count=len(facts['bodies']),
                          simulated_com_failure=True)
            save()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('document', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    asyncio.run(main(args.document.resolve(), args.output.resolve()))
