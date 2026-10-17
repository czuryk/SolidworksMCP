"""Pure dependency planning for incremental native shape changes."""
import json
from .shape_equations import topology


# These nodes read the live body of their first seed while they are built.
LIVE_HOST_KINDS={'surface_patch','anchored_extrude'}
# These copy the live bodies of all their seeds.
LIVE_SEED_KINDS={'mirror','pattern'}


def hidden_host_consumers(new_specs,changed):
    """Preserved nodes that keep the host of a node to build consumed.

    A saved Boolean or placement that already absorbed the host leaves it
    without a live body. Rebuilding that consumer restores the body first; the
    program order then builds the new node before the consumer.
    """
    hosts={s['seeds'][0] for s in new_specs if s['id'] in changed and s['kind'] in LIVE_HOST_KINDS}
    hosts|={seed for s in new_specs if s['id'] in changed and s['kind'] in LIVE_SEED_KINDS for seed in s['seeds']}
    return {s['id'] for s in new_specs if s['id'] not in changed and hosts & set(
        s['seeds'] if s['kind'] in {'combine','place'} else s.get('consumes') or [])}


def reconcile_plan(old_specs,new_specs):
    old={s['id']:s for s in old_specs};new={s['id']:s for s in new_specs}
    old_top={s['id']:s for s in topology(old_specs)}
    new_top={s['id']:s for s in topology(new_specs)}
    changed=set(old)^set(new)
    changed.update(k for k in old.keys() & new.keys()
                   if json.dumps(old_top[k],sort_keys=True)!=json.dumps(new_top[k],sort_keys=True))
    # A changed solid invalidates dependent native Boolean/Mirror selections.
    # The union of both graphs also handles removed or retargeted dependencies.
    while True:
        more={s['id'] for s in old_specs+new_specs if set(s.get('seeds') or []) & changed}
        more|=hidden_host_consumers(new_specs,changed)
        if more.issubset(changed):break
        changed.update(more)
    return {'remove':[s['id'] for s in reversed(old_specs) if s['id'] in changed],
            'build':[s['id'] for s in new_specs if s['id'] in changed],
            'preserve':[s['id'] for s in new_specs if s['id'] not in changed],
            'deleted':[s['id'] for s in old_specs if s['id'] not in new],
            'added':[s['id'] for s in new_specs if s['id'] not in old]}
