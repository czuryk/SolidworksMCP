"""Program budgets and expansion estimates, independent of the geometry hash."""

from .shape_program import ShapeProgram

MAX_DRAFT_POINTS = 2_000_000
EXPANSION_WARNING = 4096


def limits():
    return {'max_components': ShapeProgram.MAX_COMPONENTS,
            'component_definition': 'One constructor node, including pattern, mirror, place and combine; '
                                    'consumed nodes count, parameters and expressions do not.',
            'pattern_count': {'min': 2, 'max': 100, 'includes_seed': True},
            'combine_expected_bodies': {'min': 1, 'max': 32},
            'max_source_characters': 150000, 'max_range_entries': 10000,
            'max_line_events': 50000, 'max_draft_points': MAX_DRAFT_POINTS,
            'draft_limit_error': 'DRAFT_COMPLEXITY_LIMIT',
            'expansion_warning_threshold': EXPANSION_WARNING,
            'note': 'Component, expanded geometry, native feature, body and mesh budgets are independent.'}


def expansion(program):
    bodies = {}
    total = 0
    for node in program['nodes']:
        g, kind = node['geometry'], node['kind']
        seeds = g.get('seeds', [])
        count = sum(bodies[s] for s in seeds)
        if kind == 'pattern':
            count *= g['count'] - 1
        elif kind == 'combine':
            count = g['expected_bodies']
        elif not seeds:
            count = 1
        bodies[node['id']] = count
        total += count
    return {'estimated_created_body_instances': total,
            'largest_component_body_instances': max(bodies.values(), default=0),
            'note': 'Recipe estimate, not a native body count or connectivity proof.'}


def component_budget(program):
    count = len(program['nodes'])
    return {'component_count': count, 'max_components': ShapeProgram.MAX_COMPONENTS,
            'remaining_components': ShapeProgram.MAX_COMPONENTS - count,
            'expanded_complexity': expansion(program)}


def expansion_warnings(program):
    estimate = expansion(program)
    if estimate['estimated_created_body_instances'] > EXPANSION_WARNING:
        return [{'code': 'EXPANDED_GEOMETRY_HIGH', **estimate,
                 'action': 'Review nested patterns and expected native build cost before creating the part.'}]
    return []
