"""Opt-in Eiffel union permutations, pattern scopes, failure rollback and revert."""

import argparse
import hashlib
import json
import math
import textwrap

from live_improvement_support import ROOT,run_script


def equivalence_code(baseline_path, candidate_path):
    """Require zero material difference in both directions at original coordinates."""
    return textwrap.dedent(f'''
        import hashlib, json, time
        from pathlib import Path
        from solidworks_mcp.automation.com_utils import com_get
        from solidworks_mcp.automation.body_relations import _operation, _valid
        documents = []
        for filename in {json.dumps([str(baseline_path), str(candidate_path)])}:
            path = Path(filename)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            opened = automation.open_document(str(path))
            assert opened['success'], opened
            current = automation.app.ActiveDoc
            assert not bool(com_get(current, 'GetSaveFlag')), 'Comparison requires saved clean parts'
            bodies = automation._get_solid_bodies(current)
            assert len(bodies) == 1, 'Comparison requires exactly one solid per part'
            _valid(bodies[0])
            documents.append((path, digest, current, bodies[0]))
        evidence = {{'method': 'IBody2.Operations2_SWBODYCUT_both_directions_untransformed_copies',
                     'cuts': [], 'sources': []}}
        for i, j in ((0, 1), (1, 0)):
            started = time.monotonic()
            volumes = _operation(documents[i][3], documents[j][3], 'SWBODYCUT')
            evidence['cuts'].append({{'direction': [i, j], 'native_error': 0,
                                     'remaining_body_volumes_mm3': volumes,
                                     'elapsed_sec': time.monotonic() - started}})
        for path, digest, current, body in documents:
            evidence['sources'].append({{'path': str(path),
                'source_preserved': digest == hashlib.sha256(path.read_bytes()).hexdigest(),
                'clean': not bool(com_get(current, 'GetSaveFlag'))}})
        evidence['equivalent'] = all(not cut['remaining_body_volumes_mm3'] for cut in evidence['cuts'])
        print(json.dumps(evidence))
    ''')


async def verify_equivalence(run, baseline_path, candidate_path):
    result = await run.expert('execute_python', code=equivalence_code(baseline_path, candidate_path))
    evidence = json.loads(result['data']['stdout'])
    run.report.setdefault('union_equivalence', []).append(evidence)
    run.save()
    assert evidence['equivalent'], evidence
    assert all(item['source_preserved'] and item['clean'] for item in evidence['sources']), evidence
    return evidence


def variants():
    source=(ROOT/'tests/fixtures/eiffel_regression/source.py').read_text(encoding='utf-8')
    old='[parts[4],parts[0],parts[1],parts[5],parts[2],parts[3],parts[8],parts[9],parts[6],parts[7],parts[10],parts[11]]'
    assert source.count(old)==1
    return {'platform_first':source,
            'supports_first':source.replace(old,'parts[:12]'),
            'reverse':source.replace(old,'[parts[11],parts[10],parts[9],parts[8],parts[7],parts[6],parts[5],parts[4],parts[3],parts[2],parts[1],parts[0]]')}


async def acceptance(run,args):
    await run.ready()
    baseline=None
    for variant,source in variants().items():
        path=run.output/f'Eiffel_Union_{variant}.SLDPRT'
        path.with_suffix('.py').write_text(source,encoding='utf-8')
        await run.call('execute_model_code',{'mode':'create','code':source,'output_path':str(path),'observe':False})
        context=await run.context()
        facts=await run.inspect(path,context['revision'])
        assert len(facts['bodies'])==1
        volume=sum(b['volume'] for b in facts['bodies'])
        if baseline:
            # GetMassProperties is numerical integration, not a geometry identity test.
            # Retain the original comparison as evidence; require empty native cuts instead.
            evidence = await verify_equivalence(run, baseline['path'], path)
            evidence['mass_integration_diagnostic'] = {
                'baseline_volume_mm3': baseline['volume'], 'candidate_volume_mm3': volume,
                'relative_tolerance': 1e-6,
                'within_original_tolerance': math.isclose(volume, baseline['volume'], rel_tol=1e-6)}
            run.save()
            for side in ('min','max'):
                assert max(abs(a-b) for a,b in zip(facts['bbox'][side],baseline['bbox'][side]))<.001, facts['bbox']
            await run.expert('open_document', filepath=str(baseline['path']))
            await run.expert('close_document', save=False)
            context=await run.context(path)
        else:
            baseline={'volume':volume,'bbox':facts['bbox'],'path':path}
        await run.call('get_model_context',{'components':['LowerTower','UpperTower'],'include_source':True})
        await run.call('export_model',{'document_path':str(path),'expected_revision':context['revision'],
            'directory':str(run.output/f'Export_{variant}'),'expected_bodies':['EiffelTower150mm'],'mesh_cleanup':'none'})
        original=hashlib.sha256(path.read_bytes()).hexdigest()
        broken=source.replace('H = model.parameter',
                              'model.tube("DisconnectedWitness",[(300,0,0),(300,10,0)],1)\nH = model.parameter')
        broken=broken.replace('[lower_tower,upper_tower]+parts[15:]',
                              '[lower_tower,upper_tower]+parts[15:]+["DisconnectedWitness"]')
        failed=await run.call('execute_model_code',{'mode':'edit','code':broken,'document_path':str(path),
            'expected_revision':context['revision'],'observe':False},allow_failure=True)
        assert not failed['success'] and failed['data']['error']['document_restored'],failed
        assert hashlib.sha256(path.read_bytes()).hexdigest()==original
        context=await run.context()
        await run.call('execute_model_code',{'mode':'edit','parameters':{'Height':151},'document_path':str(path),
            'expected_revision':context['revision'],'observe':False})
        context=await run.context()
        await run.call('execute_model_code',{'mode':'revert','document_path':str(path),
            'expected_revision':context['revision'],'observe':False})
        assert hashlib.sha256(path.read_bytes()).hexdigest()==original
        await run.expert('close_document',save=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    run_script(acceptance,parser.parse_args())
