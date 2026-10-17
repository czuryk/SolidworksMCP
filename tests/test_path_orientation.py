"""Interpolated path segments are drawn in the direction SolidWorks solves without a warning.

Measured in SW2026: a 3D-sketch equation spline is over defined when the tangent at its end is
parallel to its chord and the tangent at its start is not (live case PATH). New paths are planned
accordingly; saved parts keep the direction their native curves were solved in.
"""
import math
import unittest

import sympy as sp

from solidworks_mcp.automation.shape_service import stored_recipes
from solidworks_mcp.shape_equations import plan_recipes, topology
from solidworks_mcp.shape_program import run_program

HEAD = 'H = model.parameter("H", 150)\nClr = model.parameter("Clr", 0.25)\nu = H / 150\n'
DIMS = {'H': 'H', 'Clr': 'Clr'}
# A vertical lead-in, a bend, and a free end: the leg of the first end-to-end retest.
LEG = [(8.6, 47.95, 2.2), (8.6, 45.6, 2.2), (10.2, 41.5, 2.6), (11.55, 36.8, 3.2), (12.7, 29.6, 3.7),
       (12.85, 23.8, 3.4)]
# Points 2..4 in line: one segment ends the straight run, the next starts it.
RUN = [(0, 0, 0), (6, 10, 2), (6, 20, 2), (6, 30, 2), (12, 40, 0)]


def tube(points, name='Leg', constructor='tube', tail='3*u'):
    text = ', '.join(f'({x}*u, {y}*u, {z}*u)' for x, y, z in points)
    return HEAD + f'model.{constructor}("{name}", [{text}], {tail})\n'


def paths(spec):
    return [s['curves'][0] for s in spec['sketches'] if s.get('role') == 'path']


def point(curve, t, H=150):
    values = []
    for expression in curve['xyz']:
        value = sp.sympify(expression.replace('"H"', str(H)).replace('"Clr"', '0.25').replace('^', '**'))
        values.append(float(value.subs(sp.Symbol('t'), t)))
    return values


def starts_at(curve, expected):
    return math.dist(point(curve, 0), expected) < 1e-9


def tangent_along_chord(curve, t):
    step = 1e-6
    a, b = point(curve, 0), point(curve, 1)
    p, q = point(curve, t - step if t else 0), point(curve, t if t else step)
    slope = [v - u for u, v in zip(p, q)]
    chord = [v - u for u, v in zip(a, b)]
    cross = math.hypot(slope[1]*chord[2] - slope[2]*chord[1], slope[2]*chord[0] - slope[0]*chord[2],
                       slope[0]*chord[1] - slope[1]*chord[0])
    return cross <= 1e-5 * math.hypot(*slope) * math.hypot(*chord)


class PathOrientationTests(unittest.TestCase):
    def plan(self, code, recipes=None):
        program = run_program(code, recipe_versions=recipes)
        return program, plan_recipes(program, DIMS)

    def test_no_segment_ends_with_its_tangent_along_the_chord(self):
        for points in (LEG, RUN):
            _, specs = self.plan(tube(points))
            for index, curve in enumerate(paths(specs[0])):
                with self.subTest(points=points[0], segment=index + 1):
                    end, start = tangent_along_chord(curve, 1), tangent_along_chord(curve, 0)
                    self.assertFalse(end and not start)

    def test_free_ends_and_straight_runs_start_where_the_tangent_is_the_chord(self):
        _, specs = self.plan(tube(LEG))
        curves = paths(specs[0])
        # The vertical lead-in keeps its direction although two coordinates are stationary at its start.
        self.assertTrue(starts_at(curves[0], LEG[0]))
        self.assertTrue(all(starts_at(curve, LEG[i]) for i, curve in enumerate(curves[:-1])))
        # The free end is drawn from the last point.
        self.assertTrue(starts_at(curves[-1], LEG[-1]))
        _, specs = self.plan(tube(RUN))
        curves = paths(specs[0])
        self.assertTrue(starts_at(curves[0], RUN[0]))
        self.assertTrue(starts_at(curves[1], RUN[2]), 'the segment that ends the straight run is reversed')
        self.assertTrue(starts_at(curves[2], RUN[2]), 'the segment that starts it is not')
        self.assertTrue(starts_at(curves[3], RUN[4]))

    def test_reversal_keeps_the_locus(self):
        legacy = {'Leg': 'path_v1'}
        _, new = self.plan(tube(LEG))
        _, old = self.plan(tube(LEG), legacy)
        for a, b in zip(paths(new[0]), paths(old[0])):
            same = math.dist(point(a, 0), point(b, 0)) < 1e-9
            for t in (0, .2, .5, .9, 1):
                self.assertLess(math.dist(point(a, t), point(b, t if same else 1 - t)), 1e-9)

    def test_new_paths_carry_the_recipe_and_saved_ones_keep_theirs(self):
        program, specs = self.plan(tube(LEG))
        self.assertEqual(program['nodes'][0]['geometry']['recipe'], 'path_chord_v2')
        self.assertEqual(specs[0]['variant'], 'path_chord_v2')
        # A part saved before the rule has no recipe in its record.
        saved = {'program': {'source_graph': {'nodes': [
            {'id': 'Leg', 'kind': 'tube', 'geometry': {'points': [0]*6}},
            {'id': 'Hose', 'kind': 'variable_tube', 'geometry': {'points': [0]*4}},
            {'id': 'Pin', 'kind': 'tube', 'geometry': {'points': [0]*2}}]}}}
        recipes = stored_recipes(saved)
        self.assertEqual(recipes, {'Leg': 'path_v1', 'Hose': 'path_v1', 'Pin': 'tube_sweep_v1'})
        program, specs = self.plan(tube(LEG), recipes)
        self.assertNotIn('recipe', program['nodes'][0]['geometry'])
        self.assertNotIn('variant', specs[0])
        self.assertNotIn('variant', topology(specs)[0])
        curves = paths(specs[0])
        # The stationary rule of saved parts: the lead-in is drawn from its far end, the free end forward.
        self.assertTrue(starts_at(curves[0], LEG[1]))
        self.assertTrue(starts_at(curves[-1], LEG[-2]))
        # And a part saved with the rule keeps it.
        kept = stored_recipes({'program': {'source_graph': {'nodes': [
            {'id': 'Leg', 'kind': 'tube', 'geometry': {'points': [0]*6, 'recipe': 'path_chord_v2'}}]}}})
        self.assertEqual(self.plan(tube(LEG), kept)[1][0]['variant'], 'path_chord_v2')

    def test_recipe_ids_of_another_kind_are_ignored(self):
        # A straight pin that grows a bend is rebuilt with the path rule; a path that becomes straight is a cylinder.
        program, _ = self.plan(tube(LEG), {'Leg': 'tube_cylinder_v3'})
        self.assertEqual(program['nodes'][0]['geometry']['recipe'], 'path_chord_v2')
        program, specs = self.plan(tube(LEG[:2]), {'Leg': 'path_v1'})
        self.assertEqual(program['nodes'][0]['geometry']['recipe'], 'tube_cylinder_v3')
        self.assertEqual(specs[0]['variant'], 'tube_cylinder_v3')
        # A name that used to be a path or a prism and is now a sectioned solid gets the current section curves.
        for stored in ('path_v1', 'path_chord_v2', 'prism_extrude_v1', 'tube_sweep_v1'):
            program = run_program('model.sectioned_solid("Leg", [(0, 5, 4), (10, 3, 3)])', recipe_versions={'Leg': stored})
            self.assertEqual(program['nodes'][0]['geometry']['profile_representation'], 'rational_quarters_v2')
        for stored in ('rational_quarters', 'trig_halves'):
            program = run_program('model.sectioned_solid("Leg", [(0, 5, 4), (10, 3, 3)])', recipe_versions={'Leg': stored})
            self.assertEqual(program['nodes'][0]['geometry']['profile_representation'], stored)

    def test_variable_tube_paths_follow_the_same_rule(self):
        radii = '[' + ', '.join(['3*u'] * len(LEG)) + ']'
        program, specs = self.plan(tube(LEG, constructor='variable_tube', tail=radii))
        self.assertEqual(program['nodes'][0]['geometry']['recipe'], 'path_chord_v2')
        self.assertTrue(starts_at(paths(specs[0])[-1], LEG[-1]))
        straight, specs = self.plan(tube(LEG[:2], constructor='variable_tube', tail='[3*u, 2*u]'))
        self.assertNotIn('recipe', straight['nodes'][0]['geometry'])
        self.assertNotIn('variant', specs[0])


if __name__ == '__main__':
    unittest.main()
