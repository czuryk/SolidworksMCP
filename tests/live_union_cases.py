"""Opt-in native union contacts, patterns, and ordered difference witnesses."""

import argparse
import math

from live_improvement_support import run_script


def cases():
    bridge = '''
left=model.tube("Left",[(-3,0,0),(-3,10,0)],.8)
right=model.tube("Right",[(3,0,0),(3,10,0)],.8)
bridge=model.tube("Bridge",[(-3,9.6,0),(3,9.6,0)],1)
'''
    for label, order in [('supports_first','left,right,bridge'),
                         ('bridge_first','bridge,left,right'),
                         ('reverse','bridge,right,left')]:
        yield label, bridge+f'model.combine("Frame",[{order}])', True, None
    for label, center, accepted, volume in [
        ('gap',(2.1,0,0),False,None), ('point',(2,2,2),False,None),
        ('edge',(2,2,0),False,None), ('face',(2,0,0),True,16),
        ('coincident',(0,0,0),True,8)]:
        code = ('a=model.rounded_prism("A",2,2,2,0)\n'
                f'b=model.rounded_prism("B",2,2,2,0,center={center!r})\n'
                'model.combine("Frame",[a,b])')
        yield label,code,accepted,volume
    contained = ('a=model.rounded_prism("A",4,4,4,0)\n'
                 'b=model.rounded_prism("B",2,2,2,0)\n')
    for label,operation,order,accepted,volume in [
        ('contained_union','union','a,b',True,64),
        ('difference','difference','a,b',True,56),
        ('difference_reversed','difference','b,a',False,None),
        ('intersection','intersection','a,b',True,8),
        ('intersection_reversed','intersection','b,a',True,8)]:
        yield label,contained+f'model.combine("Frame",[{order}],operation="{operation}")',accepted,volume
    yield 'pattern_supports', '''
left=model.tube("Left",[(-3,0,0),(-3,10,0)],.8)
supports=model.pattern("Supports",[left],count=2,step=(6,0,0))
bridge=model.tube("Bridge",[(-3,9.6,0),(3,9.6,0)],1)
model.combine("Frame",[left,supports,bridge])
''',True,None
    yield 'multiple_supports_reference', '''
left=model.tube("Left",[(-3,0,0),(-3,10,0)],.8)
middle=model.tube("Middle",[(0,0,0),(0,10,0)],.8)
right=model.tube("Right",[(3,0,0),(3,10,0)],.8)
bridge=model.tube("Bridge",[(-3,9.6,0),(3,9.6,0)],1)
model.combine("Frame",[left,middle,right,bridge])
''',True,'multiple_supports'
    yield 'pattern_multiple_supports', '''
left=model.tube("Left",[(-3,0,0),(-3,10,0)],.8)
supports=model.pattern("Supports",[left],count=3,step=(3,0,0))
bridge=model.tube("Bridge",[(-3,9.6,0),(3,9.6,0)],1)
model.combine("Frame",[left,supports,bridge])
''',True,'multiple_supports'


async def acceptance(run,args):
    await run.ready()
    bridge_volume=None
    reference_volumes={}
    run.report['cases']=[]
    for label,code,accepted,volume in cases():
        if args.cases and label not in args.cases:
            continue
        path=run.output/f'Union_{label}.SLDPRT'
        path.with_suffix('.py').write_text(code,encoding='utf-8')
        await run.call('execute_model_code',{'mode':'analyze','code':code})
        result=await run.call('execute_model_code',{'mode':'create','code':code,
                              'output_path':str(path),'observe':False},allow_failure=True)
        assert result['success']==accepted,(label,result)
        evidence={'name':label,'expected_success':accepted,'result':result}
        if accepted:
            context=await run.context()
            facts=await run.inspect(path,context['revision'])
            assert len(facts['bodies'])==1,facts
            body=facts['bodies'][0]
            actual=body.get('volume_exact',body['volume'])
            if isinstance(volume,str):
                expected=reference_volumes.setdefault(volume,actual)
                assert math.isclose(actual,expected,rel_tol=1e-6),(label,actual,expected)
            elif volume is not None:
                assert math.isclose(actual,volume,rel_tol=1e-6),(label,actual,volume)
            elif bridge_volume is None:
                bridge_volume=actual
            else:
                assert math.isclose(actual,bridge_volume,rel_tol=1e-6),(label,actual,bridge_volume)
            evidence['inspection']=facts
            await run.call('get_model_context',{'components':['Frame']})
            await run.expert('close_document',save=False)
        else:
            assert not path.exists(),path
            assert result['data']['error']['code'] in {
                'NATIVE_BOOLEAN_FAILED','NATIVE_BOOLEAN_DISCONNECTED','NATIVE_BODY_INVALID'},result
        run.report['cases'].append(evidence)
        run.save()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    parser.add_argument('--cases',nargs='+',choices=[item[0] for item in cases()])
    run_script(acceptance,parser.parse_args())
