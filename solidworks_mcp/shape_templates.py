"""Vetted shape programs for common printable joints and parts.

Each template is a complete program that builds as written and is meant to be
pasted into a larger program and adapted: sizes are plain numbers at the top,
the print clearance is the native parameter Clr so it stays constant when the
model is scaled. `fits` names the body pairs whose clearance must equal Clr;
the live acceptance test builds every template and measures them.
"""

from __future__ import annotations

TEMPLATES = {
    'pin_socket': {
        'purpose': 'Peg in a blind socket with radial and end clearance; two bodies',
        'bodies': ['Socket', 'Peg'],
        'fits': [('Socket', 'Peg')],
        'code': (
            "# Peg that slides into a blind socket. Bodies: Socket, Peg.\n"
            "Clr = model.parameter('Clr', 0.25)\n"
            "r, engaged, floor = 3, 10, 4            # peg radius, engaged length, material under the socket\n"
            "top = floor + engaged\n"
            "block = model.rounded_prism('Block', 20, 20, top, 2, center=(0, top/2, 0), axis='Y')\n"
            "hole = model.tube('Hole', [(0, floor - Clr, 0), (0, top + 1, 0)], r + Clr)\n"
            "model.combine('Socket', [block, hole], operation='difference')\n"
            "model.tube('Peg', [(0, floor, 0), (0, top + 10, 0)], r)\n"),
    },
    'inlay': {
        'purpose': 'Flush insert in a flat face, clearance all round and under it; two bodies',
        'bodies': ['Plate', 'Inlay'],
        'fits': [('Plate', 'Inlay')],
        'code': (
            "# Flush insert in the front face (z = thick) of a plate. Bodies: Plate, Inlay.\n"
            "Clr = model.parameter('Clr', 0.25)\n"
            "w, h, deep, corner, thick = 20, 12, 2, 2, 6\n"
            "blank = model.rounded_prism('Blank', 40, 30, thick, 3, center=(0, 0, thick/2))\n"
            "# The pocket tool reaches 1 mm past the face so the cut never ends on it.\n"
            "pocket = model.rounded_prism('Pocket', w + 2*Clr, h + 2*Clr, deep + Clr + 1, corner + Clr,\n"
            "                             center=(0, 0, thick + 1 - (deep + Clr + 1)/2))\n"
            "model.combine('Plate', [blank, pocket], operation='difference')\n"
            "model.rounded_prism('Inlay', w, h, deep, corner, center=(0, 0, thick - deep/2))\n"),
    },
    'hinge': {
        'purpose': 'Print-in-place barrel hinge along X: three knuckles, captive pin; two bodies',
        'bodies': ['LeafA', 'LeafB'],
        'fits': [('LeafA', 'LeafB')],
        'code': (
            "# Barrel hinge on the X axis through the origin. LeafA carries the pin and the two outer\n"
            "# knuckles, LeafB the middle knuckle with the bore. Every gap between the bodies is Clr.\n"
            "Clr = model.parameter('Clr', 0.3)\n"
            "L, R, pin, t, leaf = 30, 4, 1.5, 3, 20    # length, barrel radius, pin radius, leaf thickness, leaf width\n"
            "k = L / 3                                 # knuckle length\n"
            "reach = R + Clr                           # the leaves start clear of the other barrel\n"
            "\n"
            "def knuckle(name, x0, x1, side):\n"
            "    barrel = model.tube(name + 'Barrel', [(x0, 0, 0), (x1, 0, 0)], R)\n"
            "    web = model.rounded_prism(name + 'Web', x1 - x0, t, reach + 1, 0,\n"
            "                              center=((x0 + x1)/2, 0, side*(reach + 1)/2))\n"
            "    return [barrel, web]\n"
            "\n"
            "plate_a = model.rounded_prism('PlateA', L, t, leaf, 0, center=(0, 0, reach + leaf/2))\n"
            "axle = model.tube('Axle', [(-L/2, 0, 0), (L/2, 0, 0)], pin)\n"
            "model.combine('LeafA', [plate_a, axle] + knuckle('A1', -L/2, -k/2 - Clr, 1)\n"
            "              + knuckle('A2', k/2 + Clr, L/2, 1))\n"
            "plate_b = model.rounded_prism('PlateB', L, t, leaf, 0, center=(0, 0, -(reach + leaf/2)))\n"
            "solid_b = model.combine('SolidB', [plate_b] + knuckle('B', -k/2, k/2, -1))\n"
            "bore = model.tube('Bore', [(-k/2 - 1, 0, 0), (k/2 + 1, 0, 0)], pin + Clr)\n"
            "model.combine('LeafB', [solid_b, bore], operation='difference')\n"),
    },
    'curved_inlay': {
        'purpose': 'Inlay that wraps round a cylinder (mouth, label, band segment) with radial walls; two bodies',
        'bodies': ['Host', 'Inlay'],
        'fits': [('Host', 'Inlay')],
        'code': (
            "# Inlay on a vertical cylinder, seen from +Z. The side walls are radial, so they meet the surface\n"
            "# squarely however far the inlay wraps; a mask projected along Z leaves slivers. Bodies: Host, Inlay.\n"
            "Clr = model.parameter('Clr', 0.25)\n"
            "R, y0, y1 = 12, 10, 18               # host radius, lower and upper edge of the inlay\n"
            "thick, recess, half = 2.5, 0, 50     # inlay thickness, depth below the surface, half angle in degrees\n"
            "s, c = sin(half*pi/180), cos(half*pi/180)\n"
            "far = 2*R                            # the sector reaches past the surface\n"
            "blank = model.revolve('Blank', [(0, R), (30, R)])\n"
            "ring = model.revolve('InlayRing', [(y0, R - recess - thick), (y1, R - recess - thick),\n"
            "                                   (y1, R - recess), (y0, R - recess)], closed=True)\n"
            "sector = model.prism('InlaySector', [(0, 0), (far*s, far*c), (0, 2*far), (-far*s, far*c)], y1 - y0 + 2,\n"
            "                     center=(0, (y0 + y1)/2, 0), axis='Y')\n"
            "model.combine('Inlay', [ring, sector], operation='intersection')\n"
            "# The pocket is the same sector one clearance larger on every mating face and open to the front.\n"
            "floor = R - recess - thick - Clr\n"
            "pocket_ring = model.revolve('PocketRing', [(y0 - Clr, floor), (y1 + Clr, floor), (y1 + Clr, R + 1),\n"
            "                                           (y0 - Clr, R + 1)], closed=True)\n"
            "pocket_sector = model.prism('PocketSector', [(0, -Clr/s), (far*s + Clr*c, far*c - Clr*s), (0, 2*far),\n"
            "                                             (-far*s - Clr*c, far*c - Clr*s)], y1 - y0 + 4,\n"
            "                            center=(0, (y0 + y1)/2, 0), axis='Y')\n"
            "pocket = model.combine('Pocket', [pocket_ring, pocket_sector], operation='intersection')\n"
            "model.combine('Host', [blank, pocket], operation='difference')\n"),
    },
    'collar': {
        'purpose': 'Loose bevelled ring around a shaft (cuff, washer, bracelet); two bodies',
        'bodies': ['Shaft', 'Collar'],
        'fits': [('Shaft', 'Collar')],
        'code': (
            "# Ring that turns freely on a vertical shaft. Bodies: Shaft, Collar.\n"
            "Clr = model.parameter('Clr', 0.3)\n"
            "r, y0, y1, wall, bevel = 5, 16, 24, 3, 1\n"
            "model.tube('Shaft', [(0, 0, 0), (0, 40, 0)], r)\n"
            "# Closed ring profile as (height, radius) points, bevelled on the outside.\n"
            "bore, rim = r + Clr, r + Clr + wall\n"
            "model.revolve('Collar', [(y0, bore), (y1, bore), (y1, rim - bevel), (y1 - bevel, rim),\n"
            "                         (y0 + bevel, rim), (y0, rim - bevel)], closed=True)\n"),
    },
    'mirrored_limb': {
        'purpose': 'Jointed limb (two segments and a ball joint) mirrored to the other side; two bodies',
        'bodies': ['ArmL', 'ArmR'],
        'fits': [],
        'code': (
            "# Left arm from shoulder to wrist with a ball at the elbow, then its mirror. Bodies: ArmL, ArmR.\n"
            "shoulder, elbow, wrist = (14, 60, 0), (22, 44, 0), (24, 26, 4)\n"
            "upper = model.tube('UpperL', [shoulder, elbow], 3)\n"
            "ball = model.ellipsoid('ElbowL', (3.6, 3.6, 3.6), center=elbow)\n"
            "lower = model.tube('LowerL', [elbow, wrist], 2.6)\n"
            "arm = model.combine('ArmL', [upper, ball, lower])\n"
            "model.mirror('ArmR', [arm], plane='Right')\n"),
    },
    'open_shell': {
        'purpose': 'Hollow cup with a near-uniform wall, open at the bottom; one body',
        'bodies': ['Shell'],
        'fits': [],
        'code': (
            "# Hollow body open at the bottom: the cavity repeats the outer sections reduced by the wall.\n"
            "# Check the real wall with inspect_model(printability=true).\n"
            "wall = 2\n"
            "sections = [(0, 20, 16), (30, 24, 18), (50, 14, 10)]      # (height, half width X, half depth Z)\n"
            "outer = model.sectioned_solid('Outer', sections)\n"
            "top = sections[-1][0]\n"
            "cavity = model.sectioned_solid('Cavity', [(-1, sections[0][1] - wall, sections[0][2] - wall)]\n"
            "                               + [(y, a - wall, b - wall) for y, a, b in sections[1:-1]]\n"
            "                               + [(top - wall, sections[-1][1] - wall, sections[-1][2] - wall)])\n"
            "model.combine('Shell', [outer, cavity], operation='difference')\n"),
    },
}

INDEX = {name: item['purpose'] for name, item in TEMPLATES.items()}
USAGE = ("Complete programs for common printable parts. model_api_lookup('template:<name>') returns the code; "
         "it builds as written. Paste it into your program, rename the components if two copies are needed, "
         "change the numbers at the top and move the result with model.place. Clr is a native parameter: "
         "the fit stays the same when the model is scaled.")
