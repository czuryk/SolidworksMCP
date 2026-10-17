"""Publish a checked saved-part bundle into a new directory in one rename."""
import hashlib
import json
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from .. import __version__
from ..mesh_acceptance import inspect_mesh
from ..mesh_cleanup import bounded_cleanup, mesh_diagnostics, write_binary_stl
from ..shape_program import ProgramError
from .com_utils import com_get
from .model_inspection import inspect_model
from .shape_observation import require_target, geometry_state, ensure_unchanged, document_revision
from .runtime import PhaseTimer

DELIVERY_SCHEMA = 'solidworks-mcp/model-delivery/v1'


def stl_leaves(names):
    """Stable STL file name per body: derived from the body name, never from body order."""
    leaves, used = {}, set()
    for name in sorted(names):
        base = re.sub(r'[^A-Za-z0-9_-]+', '_', name).strip('_')[:60] or 'Body'
        leaf, suffix = base, 2
        # Names that differ only in stripped characters or case share one file system name.
        while leaf.casefold() in used:
            leaf, suffix = f'{base}_{suffix}', suffix + 1
        used.add(leaf.casefold())
        leaves[name] = leaf
    return leaves


def own_delivery(target):
    """Whether a directory is exactly a delivery this tool published earlier."""
    try:
        manifest = json.loads((target / 'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('schema') != DELIVERY_SCHEMA:
            return False, ['manifest.json has another schema']
        expected = {'manifest.json'} | {str(item['path']).replace('\\', '/') for item in manifest['files']}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False, ['no readable delivery manifest']
    actual = {path.relative_to(target).as_posix() for path in target.rglob('*') if path.is_file()}
    foreign = sorted(actual - expected)
    return not foreign, foreign[:20]


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_step_units(path):
    # This exporter promises millimetres and never changes global SW options.
    # Reject an ambiguous/different native exporter configuration.
    text = Path(path).read_text(encoding='ascii', errors='replace').upper()
    units = re.findall(r'SI_UNIT\s*\(\s*([^,]+)\s*,\s*\.METRE\.\s*\)', text)
    if not units or any(unit.strip() != '.MILLI.' for unit in units):
        raise ProgramError('STEP_UNITS_UNVERIFIED', 'STEP must declare millimetres for every SI length unit')
    if 'CONVERSION_BASED_UNIT' in text and 'LENGTH_UNIT' in text:
        raise ProgramError('STEP_UNITS_UNVERIFIED', 'Mixed conversion-based units require expert inspection')
    return 'mm'


def export_model(automation, document_path, expected_revision, directory,
                 expected_bodies=None, deviation_mm=.02, mesh_cleanup='none', diagnostics_directory=None,
                 replace=False):
    timer = PhaseTimer()
    timer.start('preflight')
    if not isinstance(directory,str) or not Path(directory).is_absolute():
        raise ProgramError('INVALID_PATH','Use a new absolute delivery directory')
    target = Path(directory).resolve()
    replacing = False
    if target.exists():
        if not replace:
            raise ProgramError('OUTPUT_EXISTS','Delivery directory already exists; existing files are preserved',
                               directory=str(target))
        owned, foreign = own_delivery(target) if target.is_dir() else (False, ['not a directory'])
        if not owned:
            raise ProgramError('OUTPUT_EXISTS',
                'replace=true only replaces a delivery published by export_model; this directory is preserved',
                directory=str(target), foreign=foreign)
        replacing = True
    if mesh_cleanup not in {'none', 'bounded'}:
        raise ProgramError('INVALID_ARGUMENT', 'mesh_cleanup must be none or bounded')
    diagnostic_target = None
    if diagnostics_directory is not None:
        if not isinstance(diagnostics_directory, str) or not Path(diagnostics_directory).is_absolute():
            raise ProgramError('INVALID_PATH', 'Use a new absolute diagnostics_directory')
        diagnostic_target = Path(diagnostics_directory).resolve()
        if diagnostic_target.exists():
            raise ProgramError('OUTPUT_EXISTS', 'Diagnostics directory already exists')
        if (diagnostic_target == target or target in diagnostic_target.parents or
                diagnostic_target in target.parents):
            raise ProgramError('INVALID_PATH', 'Delivery and diagnostics directories must be separate')
    if not isinstance(deviation_mm,(int,float)) or isinstance(deviation_mm,bool) or not .001<=deviation_mm<=.1:
        raise ProgramError('INVALID_ARGUMENT','deviation_mm must be in [.001,.1]')
    if expected_bodies is not None and (not isinstance(expected_bodies,list) or not expected_bodies or
        any(not isinstance(n,str) or not n for n in expected_bodies) or len(set(expected_bodies))!=len(expected_bodies)):
        raise ProgramError('INVALID_ARGUMENT','expected_bodies must contain unique exact body names')
    doc, record, revision = require_target(automation,document_path,expected_revision)
    source = Path(document_path)
    if not source.is_file() or bool(com_get(doc,'GetSaveFlag')):
        raise ProgramError('DOCUMENT_DIRTY','Save manual edits before exporting the native part')
    if int(com_get(doc.Extension,'NeedsRebuild2')) != 0:
        raise ProgramError('REBUILD_REQUIRED','Rebuild and save the source before delivery')
    if com_get(doc,'GetActiveSketch2') is not None:
        raise ProgramError('ACTIVE_SKETCH','Close the active sketch before export')
    state = geometry_state(doc); source_sha=file_hash(source)
    bodies=automation._get_solid_bodies(doc)
    names=[str(com_get(b,'Name')) for b in bodies]
    if not bodies or len(bodies)>64 or len(set(names))!=len(names):
        raise ProgramError('INVALID_BODY_SET','Delivery requires 1–64 uniquely named solid bodies')
    if any(not bool(com_get(b,'Visible')) for b in bodies):
        raise ProgramError('HIDDEN_BODY','Show all intended delivery bodies before export')
    if expected_bodies is not None and set(names)!=set(expected_bodies):
        raise ProgramError('BODY_SET_MISMATCH','Native body set differs from expected_bodies',actual=names)
    facts=inspect_model(automation,document_path,revision,precision='delivery')
    by_name={b['name']:b for b in facts['bodies']}
    for fact in facts['bodies']:
        if not fact['kernel_health']['passed'] or not fact.get('volume',0)>0:
            from .model_inspection import kernel_faults
            faulty=[item for item in kernel_faults(automation,doc,(record or {}).get('native',{}).get('ownership'))
                    if item['body']==fact['name']]
            raise ProgramError('NATIVE_BODY_INVALID','Native body failed kernel/volume checks: '+fact['name']+
                               (' (faulty faces of '+', '.join(faulty[0]['components'])+')'
                                if faulty and faulty[0]['components'] else ''),
                               body=fact['name'],**({'faults':faulty[0]} if faulty else {}))
    target.parent.mkdir(parents=True,exist_ok=True)
    staging=Path(tempfile.mkdtemp(prefix='.solidworks-mcp-delivery-',dir=target.parent))
    stage='native_copy'; committed=False
    files=[]
    def progress(label,**details):
        nonlocal stage
        stage=label;timer.start(label);automation._runtime.record_progress(label,**details)
    leaves=stl_leaves(names)
    def checked_file(relative,kind,inspection=None,body=None):
        path=staging/relative
        entry={'path':relative,'kind':kind,'sha256':file_hash(path),'size_bytes':path.stat().st_size}
        if inspection is not None:entry['inspection']=inspection
        if body is not None:entry['body']=body
        files.append(entry)
    def unchanged():
        ensure_unchanged(automation,doc,state)
        if bool(com_get(doc,'GetSaveFlag')) or file_hash(source)!=source_sha or document_revision(doc,record)!=revision:
            raise ProgramError('EXPORT_SOURCE_CHANGED','Source changed during export; delivery was not published')
    def failed_mesh(path, name, triangles, inspection, cleanup_error=None):
        details = {'body':name, 'inspection':inspection, 'raw_sha256':file_hash(path),
                   'mesh_cleanup':mesh_cleanup, 'diagnostics':mesh_diagnostics(triangles)}
        if cleanup_error is not None:
            details['cleanup_error'] = {'code':getattr(cleanup_error,'code','MESH_CLEANUP_FAILED'),
                                        'message':str(cleanup_error),
                                        'details':getattr(cleanup_error,'details',{})}
        if diagnostic_target is not None:
            # Publish a separate failure bundle, never a partial delivery.
            diagnostic_target.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix='.solidworks-mcp-diagnostics-', dir=diagnostic_target.parent))
            try:
                shutil.copyfile(path, temporary/'raw.stl')
                (temporary/'diagnostics.json').write_text(json.dumps({
                    'status':'EXPORT_REJECTED', 'source_sha256':source_sha, **details}, indent=2), encoding='utf-8')
                if diagnostic_target.exists():
                    raise ProgramError('OUTPUT_EXISTS', 'Diagnostics directory appeared during export')
                temporary.rename(diagnostic_target)
                details['diagnostics_directory'] = str(diagnostic_target)
            except Exception as exc:
                details['diagnostics_write_error'] = str(exc)
            finally:
                if temporary.is_dir() and temporary.parent == diagnostic_target.parent:
                    shutil.rmtree(temporary)
        raise ProgramError('STL_VALIDATION_FAILED', 'Exported mesh failed structural checks', **details)
    try:
        progress('native_copy')
        shutil.copyfile(source,staging/'model.SLDPRT')
        checked_file('model.SLDPRT','sldprt',automation._inspect_sldprt(str(staging/'model.SLDPRT')))
        if files[-1]['sha256']!=source_sha:raise ProgramError('NATIVE_COPY_MISMATCH','Native copy differs from source')
        unchanged();progress('step_export')
        doc.ClearSelection2(True)
        result=automation.export_file(str(staging/'model.step'))
        if not result.get('success'):raise ProgramError('STEP_EXPORT_FAILED',result.get('message','STEP failed'))
        unchanged()
        step=automation._inspect_step(str(staging/'model.step'))
        if step['solid_body_count']!=len(bodies) or step['closed_shell_count']<len(bodies):
            raise ProgramError('STEP_BODY_MISMATCH','STEP solid/shell count differs from native bodies',inspection=step)
        step['unit']=verify_step_units(staging/'model.step');checked_file('model.step','step',step)
        settings=automation._resolve_stl_settings({'quality':'custom','binary':True,'preserve_origin':True,
            'deviation_mm':deviation_mm,'angle_tolerance_deg':10,'max_triangles':1000000},'mm')
        # Name order, not feature-tree order: a rebuild may reorder bodies.
        for i,(name,body) in enumerate(sorted(zip(names,bodies),key=lambda item:item[0])):
            progress('body_stl',body=name,index=i,total=len(bodies))
            unchanged()
            leaf=leaves[name]
            relative=f'stl/{leaf}.stl';path=staging/relative
            export=automation._export_body_stl(doc,body,str(path),settings)
            progress('stl_validation',body=name,index=i,total=len(bodies))
            triangles=automation._load_stl_triangles(str(path),1000000)
            mesh=inspect_mesh(triangles,by_name[name]['volume'],by_name[name]['bbox'],deviation_mm)
            cleanup = None
            if not mesh['passed']:
                if mesh_cleanup == 'none':
                    failed_mesh(path, name, triangles, mesh)
                raw_sha = file_hash(path)
                try:
                    cleaned, cleanup = bounded_cleanup(triangles, by_name[name]['volume'], by_name[name]['bbox'], deviation_mm)
                except Exception as exc:
                    failed_mesh(path, name, triangles, mesh, exc)
                raw_relative = f'diagnostics/{leaf}_raw.stl'
                raw_path = staging/raw_relative
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, raw_path)
                checked_file(raw_relative, 'raw_stl_diagnostic', mesh, name)
                write_binary_stl(path, cleaned)
                reread = automation._load_stl_triangles(str(path), 1000000)
                verified = inspect_mesh(reread, by_name[name]['volume'], by_name[name]['bbox'], deviation_mm)
                if not verified['passed']:
                    failed_mesh(raw_path, name, triangles, mesh,
                                ProgramError('MESH_CLEANUP_FAILED', 'Written STL failed read-back validation', inspection=verified))
                cleanup.update(raw_sha256=raw_sha, output_sha256=file_hash(path), raw_artifact=raw_relative,
                               raw_inspection=mesh)
                mesh = {**verified, 'repair_performed':cleanup['repair_performed']}
            checked_file(relative,'stl',mesh,name)
            files[-1]['tessellation']=(export or {}).get('tessellation',{})
            if cleanup is not None:
                files[-1]['cleanup'] = cleanup
        unchanged();progress('manifest')
        manifest={'schema':DELIVERY_SCHEMA,'mcp_version':__version__,
            'source':{'path':str(source),'sha256':source_sha,'revision':revision,'configuration':state['configuration']},
            'unit':'mm','files':files,'native_inspection':facts,'stl_settings':settings,'mesh_cleanup':mesh_cleanup,
            'acceptance':{'status':'EXPORT_VALIDATED','native_kernel':'PASS','mesh_structure':'PASS',
                'shape_likeness':'NOT_CHECKED','native_parameter_cycle':'NOT_CHECKED',
                'manufacturing_clearances':'NOT_CHECKED','wall_thickness':'NOT_CHECKED',
                'mesh_self_intersections':'NOT_CHECKED','unity_rig':'NOT_CHECKED'},
            'native_copy':'identical_saved_bytes',
            'commit_method':'replace_own_delivery_rename' if replacing else 'new_directory_rename'}
        (staging/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
        manifest_sha=file_hash(staging/'manifest.json')
        for item in files:
            if file_hash(staging/item['path'])!=item['sha256']:raise ProgramError('ARTIFACT_CHANGED','A staged file changed')
        unchanged();progress('atomic_publish')
        # Windows rename fails when the destination exists; it never replaces
        # an existing delivery directory. Staging is on the same volume.
        if replacing:
            # Move the previous delivery aside, publish, then drop it; restore it if publishing fails.
            owned,foreign=own_delivery(target)
            if not owned:raise ProgramError('OUTPUT_EXISTS','Delivery directory changed during export',foreign=foreign)
            retired=target.with_name('.solidworks-mcp-replaced-'+uuid.uuid4().hex)
            target.rename(retired)
            try:
                staging.rename(target);committed=True
            except Exception:
                retired.rename(target)
                raise
            shutil.rmtree(retired,ignore_errors=True)
        else:
            if target.exists():raise ProgramError('OUTPUT_EXISTS','Delivery directory appeared during export')
            staging.rename(target);committed=True
        automation._runtime.increment('cad_result_files',len(files)+1)
        return {'status':'EXPORT_VALIDATED','directory':str(target),'manifest':str(target/'manifest.json'),
                'manifest_sha256':manifest_sha,'files':files,'acceptance':manifest['acceptance'],
                'document_path':str(source),'revision':revision,'source_preserved':True,
                'replaced_previous_delivery':replacing,'timings_ms':timer.result()}
    except ProgramError as exc:
        exc.details.update(delivery_committed=committed, partial_files=[item['path'] for item in files],
                           staging_directory=str(staging), partial_delivery_published=committed,
                           timings_ms=timer.result(), runtime=automation._runtime.reader_snapshot())
        exc.details.setdefault('stage', stage)
        from .session_identity import solidworks_processes, process_private_bytes, OWN_PROCESS
        try:
            processes = solidworks_processes()
            exc.details['processes'] = {'mcp': {**OWN_PROCESS, 'private_bytes': process_private_bytes(OWN_PROCESS['pid'])},
                                       'solidworks': [{**p, 'private_bytes': process_private_bytes(p['pid'])} for p in processes]}
        except OSError as probe_error:
            exc.details['process_probe_error'] = str(probe_error)
        raise
    except Exception as exc:
        from .com_utils import is_rpc_disconnect
        raise ProgramError('RPC_CONNECTION_LOST' if is_rpc_disconnect(exc) else 'EXPORT_FAILED', str(exc),
                           com_hresult=getattr(exc, 'hresult', None), stage=stage, delivery_committed=committed,
                           partial_files=[item['path'] for item in files], staging_directory=str(staging),
                           timings_ms=timer.result(), runtime=automation._runtime.reader_snapshot()) from exc
    finally:
        # Only remove the unique directory allocated by this call.
        if not committed and staging.is_dir() and staging.parent==target.parent:
            shutil.rmtree(staging)
