"""Opt-in native recipe smoke test. Only creates a new explicitly named part."""

import argparse
import json
import math
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.automation.session_lease import SessionLease
from solidworks_mcp.automation.com_utils import com_get


PROGRAM = """
H = model.parameter("H", 150)
def limb(name, x):
    return model.tube(name, [(x, .32*H, 0), (x+.03*H, .19*H, -.025*H),
                             (x+.025*H, .055*H, 0)], .021*H)
model.sectioned_solid("Torso", [(.31*H, .097*H, .080*H),
                                (.60*H, .126*H, .103*H)])
leg = limb("Leg_Left", .055*H)
foot = model.dome("Foot_Left", radius=.071*H, height=.064*H,
                 center=(leg.end[0], 0, leg.end[2]))
model.mirror("Leg_Right", [leg, foot], plane="Right")
"""


def verify_saved(sw, output):
    """Reopen the saved B-rep and compare independent mass invariants."""
    path = Path(output).resolve()
    record = json.loads(path.with_suffix('.shape.json').read_text(encoding='utf-8'))
    assert sw.connect()['success']
    existing = sw.app.GetOpenDocumentByName(str(path))
    if existing is not None:
        sw.app.CloseDoc(sw._get_doc_title(existing))
    opened = sw.open_document(str(path))
    assert opened['success'], opened
    doc = sw.app.ActiveDoc
    assert Path(sw._get_doc_path(doc)).resolve() == path
    assert bool(com_get(doc, 'EditRebuild3'))
    bodies = sw._get_solid_bodies(doc)
    assert len(bodies) == 5, len(bodies)
    props = {str(com_get(b, 'Name')): sw._body_mass_props(b, 'mm') for b in bodies}
    assert all(p and p['volume_exact'] > 0 for p in props.values()), props
    nodes = {n['id']: n['geometry'] for n in record['program']['nodes']}
    (y0,a0,b0),(y1,a1,b1) = nodes['Torso']['sections']
    expected_torso = math.pi*(y1-y0)*(2*a0*b0+a0*b1+a1*b0+2*a1*b1)/6
    assert math.isclose(props['Torso']['volume_exact'], expected_torso, rel_tol=.001)
    r,h = nodes['Foot_Left']['radius'], nodes['Foot_Left']['height']
    expected_cap = math.pi*h*(3*r*r+h*h)/6
    # Equation curves are kernel spline approximations of the analytical cap.
    cap_tolerance=.001 if record['native'].get('compiler_version',0)>=2 else 1e-6
    assert math.isclose(props['Foot_Left']['volume_exact'], expected_cap, rel_tol=cap_tolerance)
    mirror_names = record['native']['components']['Leg_Right']['bodies']
    remaining = list(mirror_names)
    for source in ['Leg_Left', 'Foot_Left']:
        seed = props[source]
        match = min(remaining, key=lambda n: abs(props[n]['volume_exact']-seed['volume_exact']))
        other = props[match]
        # B-spline mass integration differs by 0.0004% after reflection in SW2026.
        # Native Mirror identity and mirrored centroid are checked separately.
        mirror_tolerance=1e-5 if record['native'].get('compiler_version',0)>=2 else 1e-8
        assert math.isclose(seed['volume_exact'], other['volume_exact'], rel_tol=mirror_tolerance)
        x,y,z = seed['center_of_mass']
        assert math.dist(other['center_of_mass'], [-x,y,z]) < .001
        remaining.remove(match)
    return {'status':'SAVED_NATIVE_GEOMETRY_VERIFIED', 'rebuild':True,
            'solid_body_count':len(bodies), 'mass_properties_mm':props,
            'analytical_torso_volume_mm3':expected_torso,
            'analytical_foot_volume_mm3':expected_cap,
            'mirror_volume_and_centroid_verified':True,
            'native_parameter_bindings':record['native']['native_parameter_bindings']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--observe", action="store_true")
    parser.add_argument("--verify-existing", action="store_true")
    parser.add_argument("--report")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s')
    sw = SolidWorksAutomation()
    with SessionLease():
        if args.verify_existing:
            result = verify_saved(sw, args.output)
            if args.report:
                Path(args.report).write_text(json.dumps(result, indent=2), encoding='utf-8')
            print(json.dumps(result, indent=2))
            print('LIVE_SHAPE_PROGRAM_REOPEN_PASS')
            return 0
        result = sw.execute_model_code(
            PROGRAM, mode="create", output_path=args.output, observe=args.observe)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    if not result.get("success"):
        return 1
    data = result["data"]
    bodies = data["body_state"].get("bodies", [])
    assert len(bodies) == 5, f"Expected five native bodies, got {len(bodies)}"
    assert set(data["components"]) == {"Torso", "Leg_Left", "Foot_Left", "Leg_Right"}
    assert data["components"]["Torso"]["kind"] == "sectioned_solid"
    assert data["components"]["Leg_Left"]["kind"] == "tube"
    assert data["native_parameter_bindings"] is True
    with SessionLease():
        data['saved_native_verification'] = verify_saved(sw, args.output)
    if args.report:
        Path(args.report).write_text(json.dumps(result, indent=2), encoding='utf-8')
    print("LIVE_SHAPE_PROGRAM_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
