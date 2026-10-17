"""Opt-in latency profile of the reference model (changes.md, P1-2).

Creates the reference part from tests/fixtures/bender_program.py in a NEW
folder, then runs three edit scenarios three times each through the same
request boundary the MCP server uses, and writes medians with timings_ms.

    python tests/live_edit_latency.py --output <new folder> --report <json>

--part reuses an existing reference part: it is copied into the new folder, so
before/after comparisons do not pay for the creation again.
"""
import argparse
import json
import shutil
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.automation.request_execution import execute_request
from solidworks_mcp.automation.request_journal import RequestJournal
from solidworks_mcp.automation.session_lease import SessionLease
from solidworks_mcp.tool_registry import dispatch_new_tool

FIXTURE = Path(__file__).parent / 'fixtures' / 'bender_program.py'
PUPIL_PRISM = 'model.rounded_prism("PupilCutL", 1.7*u, 1.7*u, 2.0*u, 0.15*u, center=P(5.6, 116.1, 17.0))'
PUPIL_WIDE = 'model.rounded_prism("PupilCutL", 1.8*u, 1.8*u, 2.0*u, 0.15*u, center=P(5.6, 116.1, 17.0))'
PUPIL_ROUND = 'model.ellipsoid("PupilCutL", (0.85*u, 0.85*u, 1.0*u), center=P(5.6, 116.1, 17.0))'
SCENARIOS = {
    # Only the driving parameter changes.
    'parameter_H': [{'parameters': {'H': 120}}, {'parameters': {'H': 150}}, {'parameters': {'H': 120}}],
    # One definition changes; the topology of every component stays.
    'definition_edit': [{'patch': [{'old': PUPIL_PRISM, 'new': PUPIL_WIDE}]},
                        {'patch': [{'old': PUPIL_WIDE, 'new': PUPIL_PRISM}]},
                        {'patch': [{'old': PUPIL_PRISM, 'new': PUPIL_WIDE}]}],
    # One component changes its family: it and its dependents (one body and its mirror) rebuild.
    'topology_edit': [{'patch': [{'old': PUPIL_WIDE, 'new': PUPIL_ROUND}]},
                      {'patch': [{'old': PUPIL_ROUND, 'new': PUPIL_PRISM}]},
                      {'patch': [{'old': PUPIL_PRISM, 'new': PUPIL_ROUND}]}],
}


def body_facts(sw):
    """Exact volume, extents and face count of every body: the geometry an edit left behind."""
    from solidworks_mcp.automation.com_utils import com_get
    from solidworks_mcp.automation.model_inspection import exact_bbox
    facts = {}
    for body in sw._get_solid_bodies(sw.app.ActiveDoc):
        box = exact_bbox(body)
        facts[str(com_get(body, 'Name'))] = (sw._body_mass_props(body, 'mm')['volume_exact'],
                                             box['min'] + box['max'], int(com_get(body, 'GetFaceCount')))
    return facts


def worst_difference(a, b):
    """Largest relative volume or absolute extent difference; body set and face counts must agree."""
    if set(a) != set(b) or any(a[name][2] != b[name][2] for name in a):
        return float('inf')
    return max(max(abs(a[n][0] - b[n][0]) / max(1., abs(b[n][0])),
                   max(abs(x - y) for x, y in zip(a[n][1], b[n][1]))) for n in a)


def call(sw, journal, arguments):
    """One mutation as the server runs it: receipt, dispatch, compact answer."""
    started = time.perf_counter()
    result = execute_request(sw, 'execute_model_code', arguments,
                             lambda: dispatch_new_tool(sw, 'execute_model_code', arguments), journal)
    return result, round(time.perf_counter() - started, 3)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', required=True, help='New folder for the part; must not exist')
    parser.add_argument('--report', required=True, help='JSON report path')
    parser.add_argument('--part', help='Existing reference .SLDPRT to copy instead of creating one')
    parser.add_argument('--observe', action='store_true', help='Capture the default views after every edit')
    parser.add_argument('--scenarios', nargs='+', choices=sorted(SCENARIOS), default=list(SCENARIOS),
                        help='Scenarios to run, in the given order')
    parser.add_argument('--runs', type=int, default=3, choices=(1, 2, 3), help='Edits per scenario')
    parser.add_argument('--verify-rebuild', action='store_true',
                        help='After every edit force a full rebuild and compare every body with the edit result')
    parser.add_argument('--keep-open', action='store_true', help='Leave the test part open in SolidWorks')
    options = parser.parse_args()
    folder = Path(options.output).resolve()
    if folder.exists():
        raise SystemExit('Output folder already exists: ' + str(folder))
    folder.mkdir(parents=True)
    # SolidWorks cannot hold two open documents with one file name: derive it from the folder.
    part = folder / (folder.name + '.SLDPRT')
    journal = RequestJournal(folder / 'requests.sqlite3')
    report = {'part': str(part), 'observe': options.observe, 'scenarios': {}}
    sw = SolidWorksAutomation()
    with SessionLease():
        assert sw.connect()['success']
        if options.part:
            source = Path(options.part)
            shutil.copyfile(source, part)
            shutil.copyfile(source.with_suffix('.shape.json'), part.with_suffix('.shape.json'))
            assert sw.open_document(str(part))['success']
            report['create'] = {'reused': str(source)}
        else:
            created, wall = call(sw, journal, {'code': FIXTURE.read_text(encoding='utf-8'), 'mode': 'create',
                                               'output_path': str(part), 'observe': options.observe})
            assert created['success'], created
            report['create'] = {'wall_sec': wall, 'timings_ms': created['data'].get('timings_ms'),
                                'bodies': len(created['data']['bodies']),
                                'answer_chars': len(json.dumps(created, default=str))}
            print('CREATE', wall, created['data'].get('timings_ms'), flush=True)
        context = dispatch_new_tool(sw, 'get_model_context', {})
        assert context['success'], context
        revision = context['data']['revision']
        report['context_chars'] = len(json.dumps(context, default=str))
        if options.part:
            # A reused part is in whatever state its last run left: return it to the fixture program.
            reset, wall = call(sw, journal, {'mode': 'edit', 'document_path': str(part), 'expected_revision': revision,
                                             'code': FIXTURE.read_text(encoding='utf-8'), 'observe': False})
            assert reset['success'], reset
            revision = reset['data']['revision']
            print('RESET', wall, reset['data'].get('rebuild_mode'), reset['data']['rebuilt_components'], flush=True)
        for name in options.scenarios:
            runs = []
            for edit in SCENARIOS[name][:options.runs]:
                result, wall = call(sw, journal, {'mode': 'edit', 'document_path': str(part),
                                                  'expected_revision': revision,
                                                  'observe': options.observe, **edit})
                assert result['success'], result
                data = result['data']
                revision = data['revision']
                runs.append({'wall_sec': wall, 'timings_ms': data['timings_ms'],
                             'rebuilt_components': data['rebuilt_components'],
                             'edit_strategy': data.get('edit_strategy'),
                             'rebuild_mode': data.get('rebuild_mode'),
                             'answer_chars': len(json.dumps(result, default=str))})
                print(name, wall, data.get('rebuild_mode'), data['timings_ms'], data['rebuilt_components'], flush=True)
                if options.verify_rebuild:
                    # The edit's own rebuild against a forced one: any difference is a stale feature.
                    edited = body_facts(sw)
                    assert sw.app.ActiveDoc.ForceRebuild3(False)
                    difference = worst_difference(edited, body_facts(sw))
                    runs[-1]['difference_from_forced_rebuild'] = difference
                    print('   difference from a forced rebuild:', difference, flush=True)
                    assert difference < 1e-6, difference
                    assert sw.save_document(str(part))['success']
                    revision = dispatch_new_tool(sw, 'get_model_context', {})['data']['revision']
            phases = sorted({key for run in runs for key in run['timings_ms']})
            report['scenarios'][name] = {
                'median_wall_sec': statistics.median(run['wall_sec'] for run in runs),
                'median_timings_ms': {key: statistics.median(run['timings_ms'].get(key, 0) for run in runs)
                                      for key in phases},
                'runs': runs}
        if not options.keep_open:
            sw.app.CloseDoc(sw._get_doc_title(sw.app.ActiveDoc))
    Path(options.report).write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('LATENCY_REPORT', json.dumps({name: item['median_wall_sec']
                                        for name, item in report['scenarios'].items()}), flush=True)


if __name__ == '__main__':
    main()
