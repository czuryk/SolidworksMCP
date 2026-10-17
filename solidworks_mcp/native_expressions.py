"""Numeric evaluation of the equation-curve expressions the compiler writes into SolidWorks.

Used to prove, after a rebuild, that every native curve reflects the current
dimension values. The expressions come from our own emitter: quoted dimension
names, numbers, t, + - * / ^, parentheses and sqr/sin/cos.
"""

from __future__ import annotations

import math
import re

_NAME = re.compile(r'"([^"]+)"')
_SAFE = re.compile(r'(?:\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+|sqrt|sin|cos|t|[-+*/() ])*')
_FUNCTIONS = {'sqrt': math.sqrt, 'sin': math.sin, 'cos': math.cos}


def referenced(expression):
    """Native dimension names an expression depends on."""
    return set(_NAME.findall(expression))


def evaluate(expression, t, values):
    """Value of one coordinate expression at curve parameter t; values maps dimension names to numbers."""
    def number(match):
        return '(' + repr(float(values[match.group(1)])) + ')'
    text = _NAME.sub(number, expression).replace('^', '**').replace('sqr(', 'sqrt(')
    if not _SAFE.fullmatch(text.replace('**', '*')):
        raise ValueError('Unsupported native expression: ' + expression[:80])
    return float(eval(text, {'__builtins__': {}}, {**_FUNCTIONS, 't': float(t)}))


def curve_endpoints(curve, values):
    """Expected (x, y, z) at both ends of a recorded native curve; 2D sketch curves have z = 0."""
    count = 3 if curve['mode'] == '3d' else 2
    points = []
    for t in curve['range']:
        point = [evaluate(curve['xyz'][i], t, values) for i in range(count)]
        points.append(tuple(point + [0.] * (3 - count)))
    return points
