"""Opt-in create/save/reopen/edit/inspect/export qualification at 128/256/512 nodes."""

import argparse
import hashlib
import json

from live_improvement_support import run_script
from solidworks_mcp.shape_program import run_program


def program(count,family):
    prefix='H=model.parameter("H",5)\nparts=[]\n'
    if family=='lattice':
        prefix+=f'parts.append(model.rounded_prism("Platform",{count},3,1,0,center=({(count-3)/2},0,0),axis="Y"))\n'
    rods=count-2 if family=='lattice' else count-1
    if family in {'straight','lattice'}:
        body='model.tube("Rod"+str(i),[(i,0,0),(i,H,0)],.65)'
    elif family=='curved':
        body='model.tube("Rod"+str(i),[(i,0,0),(i+.15,H/2,0),(i,H,0)],.65)'
    else:
        body='model.sectioned_solid("Rod"+str(i),[(0,.65,.7),(H,.65,.7)],center_xz=(i,0))'
    code=prefix+f'for i in range({rods}):\n    parts.append({body})\nmodel.combine("Frame",parts,expected_bodies=1)\n'
    assert len(run_program(code)['nodes'])==count
    return code


async def acceptance(run,args):
    await run.ready()
    for topic in ('index','all','limits'):
        data=(await run.call('model_api_lookup',{'topic':topic}))['data']
        assert data['limits']['max_components']==512,data
    invalid=program(512,'straight').replace('range(511)','range(512)')
    rejected=await run.call('execute_model_code',{'mode':'create','code':invalid,
        'output_path':str(run.output/'Rejected513.SLDPRT'),'observe':False},allow_failure=True)
    assert not rejected['success'] and rejected['data']['error']['code']=='PROGRAM_LIMIT',rejected
    assert not (run.output/'Rejected513.SLDPRT').exists()
    for count in args.counts:
        for family in args.families:
            code=program(count,family)
            path=run.output/f'Limit_{count}_{family}.SLDPRT'
            path.with_suffix('.py').write_text(code,encoding='utf-8')
            analyzed=await run.call('execute_model_code',{'mode':'analyze','code':code})
            assert analyzed['data']['component_count']==count
            created=await run.call('execute_model_code',{'mode':'create','code':code,'output_path':str(path),'observe':False})
            await run.expert('close_document',save=False)
            context=await run.context(path)
            assert context['component_count']==count,context
            baseline=await run.inspect(path,context['revision'])
            for height in (5.5,5.):
                edited=await run.call('execute_model_code',{'mode':'edit','document_path':str(path),
                    'expected_revision':context['revision'],'parameters':{'H':height},'observe':False})
                context=await run.context()
                facts=await run.inspect(path,context['revision'])
                assert len(facts['bodies'])==1,facts
            assert abs(sum(b['volume'] for b in facts['bodies'])-sum(b['volume'] for b in baseline['bodies']))<.001
            await run.call('export_model',{'document_path':str(path),'expected_revision':context['revision'],
                'directory':str(run.output/f'Export_{count}_{family}'),'expected_bodies':['Frame'],'mesh_cleanup':'none'})
            await run.expert('close_document',save=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    parser.add_argument('--counts',type=int,nargs='+',default=[128,256,512],choices=[128,256,512])
    parser.add_argument('--families',nargs='+',default=['straight','curved','loft','lattice'],
                        choices=['straight','curved','loft','lattice'])
    run_script(acceptance,parser.parse_args())
