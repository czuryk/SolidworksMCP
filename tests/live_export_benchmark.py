"""Opt-in repeated exports of a hash-locked copy; fresh/repeated/after-edit modes."""

import argparse
import hashlib
import json
import os
import shutil
import statistics
import time
from pathlib import Path

from live_improvement_support import ROOT,run_script


async def acceptance(run,args):
    await run.ready()
    source=Path(args.source).resolve()
    manifest=json.loads((ROOT/'tests/fixtures/eiffel_regression/manifest.json').read_text(encoding='utf-8'))
    original_hash=hashlib.sha256(source.read_bytes()).hexdigest()
    assert original_hash==manifest['source']['sha256'],'Control file differs from the saved Eiffel fixture'
    path=run.output/'Benchmark_Eiffel.SLDPRT'
    shutil.copyfile(source,path)
    run.report.update(control_sha256=original_hash,control_revision=manifest['source']['revision'],
                      mode=args.mode,reader=args.reader,export_settings={'deviation_mm':.02,'mesh_cleanup':'none'})
    os.environ['SOLIDWORKS_MCP_TESSELLATION_READER']=args.reader
    context=await run.context(path)
    baseline=await run.inspect(path,context['revision'])
    if args.mode=='after-edits':
        for index in range(args.edit_cycles):
            await run.call('execute_model_code',{'mode':'edit','document_path':str(path),
                'expected_revision':context['revision'],'parameters':{'Height':151},'observe':False})
            context=await run.context()
            await run.inspect(path,context['revision'])
            await run.call('execute_model_code',{'mode':'revert','document_path':str(path),
                'expected_revision':context['revision'],'observe':False})
            context=await run.context()
            assert hashlib.sha256(path.read_bytes()).hexdigest()==original_hash,'Revert did not restore exact baseline bytes'
    durations=[]
    for index in range(args.runs):
        if args.mode=='fresh':
            await run.expert('close_document',save=False)
            await run.expert('restart_solidworks')
            context=await run.context(path)
        assert hashlib.sha256(path.read_bytes()).hexdigest()==original_hash
        started=time.perf_counter()
        target=run.output/f'Export_{index+1:02d}'
        result=await run.call('export_model',{'document_path':str(path),'expected_revision':context['revision'],
            'directory':str(target),'expected_bodies':['EiffelTower150mm'],'mesh_cleanup':'none','deviation_mm':.02})
        durations.append(time.perf_counter()-started)
        actual=json.loads((target/'manifest.json').read_text(encoding='utf-8'))
        stl=next(item for item in actual['files'] if item['kind']=='stl')
        assert stl['tessellation']['facet_count']==186152,stl['tessellation']
        assert not stl['inspection']['repair_performed']
        assert actual['acceptance']['native_kernel']=='PASS' and actual['acceptance']['mesh_structure']=='PASS'
        assert actual['source']['sha256']==original_hash
        run.report.setdefault('exports',[]).append({'elapsed_sec':durations[-1],
            'revision':actual['source']['revision'],'tessellation':stl['tessellation'],
            'status':(await run.call('get_executor_status',{}))['data']})
        run.save()
    facts=await run.inspect(path,context['revision'])
    assert facts['bbox']==baseline['bbox']
    assert len(facts['bodies'])==1
    run.report['median_export_sec']=statistics.median(durations)
    run.report['slowdown_vs_first']=max(durations)/durations[0]
    await run.expert('close_document',save=False)
    assert hashlib.sha256(source.read_bytes()).hexdigest()==original_hash


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    parser.add_argument('--source',required=True,help='Immutable control_model.SLDPRT copy')
    parser.add_argument('--mode',choices=['fresh','repeated','after-edits'],default='repeated')
    parser.add_argument('--runs',type=int,default=10)
    parser.add_argument('--edit-cycles',type=int,default=5)
    parser.add_argument('--reader',choices=['compat','bound'],default='compat')
    args=parser.parse_args()
    if args.runs<1 or args.edit_cycles<5:parser.error('Use at least one export and five edit cycles')
    run_script(acceptance,args)
