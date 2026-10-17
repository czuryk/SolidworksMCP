H = model.parameter("H", 150)
Clr = model.parameter("Clr", 0.25)
u = H / 150

def P(x, y, z):
    return tuple(0 if c == 0 else c * u for c in (x, y, z))

# ---------------- Torso ----------------
torso_base = model.sectioned_solid("TorsoBase", [(46.5*u, 13.4*u, 11.9*u), (47.2*u, 14.7*u, 13.1*u), (48.5*u, 15.3*u, 13.6*u)])
torso_body = model.sectioned_solid("TorsoBody", [(48.5*u, 15.3*u, 13.6*u), (90.0*u, 19.4*u, 16.0*u)])
torso_cone = model.sectioned_solid("TorsoCone", [(90.0*u, 19.4*u, 16.0*u), (97.0*u - Clr, 11.4*u, 11.4*u)])
cup_seed = model.dome("ShoulderCupSeed", radius=5.5*u, height=5.5*u)
cup_l = model.place("ShoulderCupL", [cup_seed], frame=model.frame(origin=P(16.0, 83.8, -5.45), rotation=(0, 0, -pi/2)))
cup_r = model.mirror("ShoulderCupR", [cup_l], plane="Right")
# Raised door panel following the curved chest surface, plus its knob.
door_mask = model.rounded_prism("DoorMask", 27.0*u, 30.5*u, 20.0*u, 3.0*u, center=P(0, 67.25, 10.0))
door = model.surface_patch("DoorPanel", torso_body, door_mask, 0.6*u)
knob = model.ellipsoid("DoorKnob", (1.3*u, 1.3*u, 1.3*u), center=P(9.5, 67.3, 12.6))
neck_peg = model.tube("NeckPeg", [P(0, 95.0, 0), (0, 101.0*u - Clr, 0)], 4.5*u)
torso_solid = model.combine("TorsoSolid", [torso_base, torso_body, torso_cone, cup_l, cup_r, door, knob, neck_peg])
# Sockets are one clearance larger than the arm and leg pegs on every mating face.
sh_hole_l = model.tube("ShoulderHoleL", [(15.5*u - Clr, 83.8*u, -5.45*u), P(24.0, 83.8, -5.45)], 3.0*u + Clr)
sh_hole_r = model.mirror("ShoulderHoleR", [sh_hole_l], plane="Right")
leg_sock_l = model.tube("LegSocketL", [P(8.9, 45.5, 3.9), P(8.9, 51.5, 3.9)], 2.4*u + Clr)
leg_sock_r = model.mirror("LegSocketR", [leg_sock_l], plane="Right")
torso = model.combine("Torso", [torso_solid, sh_hole_l, sh_hole_r, leg_sock_l, leg_sock_r], operation="difference")

# ---------------- Head ----------------
head_cyl = model.sectioned_solid("HeadCylinder", [(97.0*u, 10.7*u, 10.7*u), (126.3*u, 10.7*u, 10.7*u)])
# Mouth: rounded opening wrapped on the head cylinder. The pocket and the teeth blank are
# curved shells cut from the same surface; the blank is one clearance smaller on every side.
mouth_mask = model.rounded_prism("MouthMask", 19.0*u, 7.6*u, 17.6*u + Clr, 2.3*u,
                                 center=(0, 103.9*u, (22.4*u - Clr) / 2))
mouth_pocket = model.surface_patch("MouthPocket", head_cyl, mouth_mask, 2.6*u + Clr, inward=True)
teeth_mask = model.rounded_prism("TeethMask", 19.0*u - 2*Clr, 7.6*u - 2*Clr, 17.6*u, 2.3*u - Clr,
                                 center=P(0, 103.9, 11.2))
teeth_blank = model.surface_patch("TeethBlank", head_cyl, teeth_mask, 2.6*u, inward=True)
head_dome = model.dome("HeadDome", radius=10.7*u, height=10.7*u, center=P(0, 126.3, 0))
# Visor: one pill-section tube extruded straight forward (flat front rim, crisp edges).
# The flat middle and the two round sides are tangent; each side tip ends in a smooth
# rounded cap where it meets the side of the head.
visor_mid = model.rounded_prism("VisorMid", 17.8*u, 12.0*u, 16.8*u, 1.0*u, center=P(0, 116.5, 7.4))
visor_side_seed = model.revolve("VisorSideSeedL", [(0, 6.0*u), (15.8*u, 6.0*u)])
visor_side_l = model.place("VisorSideL", [visor_side_seed],
                           frame=model.frame(origin=P(7.9, 116.5, 0), rotation=(pi/2, 0, 0)))
visor_cap_seed = model.ellipsoid("VisorCapSeedL", (6.0*u, 2.7*u, 6.0*u))
visor_cap_l = model.place("VisorCapL", [visor_cap_seed],
                          frame=model.frame(origin=P(7.9, 116.5, 0), rotation=(pi/2, 0, 0)))
visor_side_r = model.mirror("VisorSideR", [visor_side_l], plane="Right")
visor_cap_r = model.mirror("VisorCapR", [visor_cap_l], plane="Right")
ant_base = model.dome("AntennaBase", radius=5.0*u, height=2.6*u, center=P(0, 135.4, 0))
ant_rod = model.revolve("AntennaRod", [(136.5*u, 1.6*u), (146.6*u, 1.0*u)])
ant_ball = model.ellipsoid("AntennaBall", (2.0*u, 2.0*u, 2.0*u), center=P(0, 148.0, 0))
head_shell = model.combine("HeadShell", [head_cyl, head_dome, visor_mid, visor_side_l, visor_side_r,
                                         visor_cap_l, visor_cap_r, ant_base, ant_rod, ant_ball])

# One shared pill-shaped eye cavity behind the front rim; closed visor sides; even rim width.
# The eye back face sits at z=10.0; the cavity floor is one clearance behind it.
eye_floor = 10.0*u - Clr
cav_a = model.rounded_prism("EyeCavity", 24.4*u, 8.6*u, 17.0*u - eye_floor, 4.2*u,
                            center=(0, 116.5*u, (17.0*u + eye_floor) / 2))
neck_socket = model.tube("NeckSocket", [P(0, 96.0, 0), P(0, 101.0, 0)], 4.5*u + Clr)
head_carved = model.combine("HeadCarved", [head_shell, cav_a, mouth_pocket, neck_socket], operation="difference")
eye_pin_l = model.tube("EyePinL", [P(5.0, 116.5, 8.5), (5.0*u, 116.5*u, 12.25*u - Clr)], 1.0*u)
eye_pin_r = model.mirror("EyePinR", [eye_pin_l], plane="Right")
head = model.combine("Head", [head_carved, eye_pin_l, eye_pin_r])

# ---------------- Eyes (seed is the character's left eye, +X) ----------------
# Wide bulbs that nearly fill the opening height and bulge about 1 mm past the rim.
eye_ball = model.ellipsoid("EyeBallL", (4.75*u, 3.85*u, 3.85*u), center=P(5.0, 116.5, 12.9))
eye_back = model.rounded_prism("EyeBackCutL", 11.0*u, 10.0*u, 10.0*u, 0.5*u, center=P(5.0, 116.5, 5.0))
eye_hole = model.tube("EyeHoleL", [P(5.0, 116.5, 9.0), P(5.0, 116.5, 12.25)], 1.0*u + Clr)
pupil = model.rounded_prism("PupilCutL", 1.7*u, 1.7*u, 2.0*u, 0.15*u, center=P(5.6, 116.1, 17.0))
# Angled upper lid over the inner half of the eye: the cut plane rises toward the outer side.
lid0 = model.rounded_prism("EyeLidCutSeedL", 12.0*u, 6.0*u, 12.0*u, 0.3*u, center=P(0, 3.0, 0))
lid = model.place("EyeLidCutL", [lid0], frame=model.frame(origin=P(5.0, 120.0, 12.9), rotation=(0, 0, 23*pi/180)))
eye_l = model.combine("EyeL", [eye_ball, eye_back, eye_hole, pupil, lid], operation="difference")
eye_r = model.mirror("EyeR", [eye_l], plane="Right")

# ---------------- Teeth insert ----------------
# Curved plate set 1.3 mm below the head surface (r 8.1..9.4) with thin tooth lines.
teeth_core = model.revolve("TeethCore", [(98.0*u, 9.4*u), (103.75*u, 9.4*u), (103.9*u, 9.2*u),
                                         (104.05*u, 9.4*u), (110.0*u, 9.4*u)])
teeth_plate = model.combine("TeethPlate", [teeth_blank, teeth_core], operation="intersection")
teeth_lines = []
for i, (x, zc) in enumerate([(0.0, 10.7), (3.1, 10.174), (-3.1, 10.174), (6.2, 8.365), (-6.2, 8.365)]):
    teeth_lines.append(model.rounded_prism("TeethLine" + str(i), 0.3*u, 9.0*u, 3.0*u, 0.1*u, center=P(x, 103.9, zc)))
teeth = model.combine("Teeth", [teeth_plate] + teeth_lines, operation="difference")

# ---------------- Arms (seed is the character's left arm, +X) ----------------
# Slim core hose plus straight raised bands between arc-length stations on the native path.
arm_peg = model.tube("ArmPegL", [P(15.5, 83.8, -5.45), P(21.0, 83.8, -5.45)], 3.0*u)
arm_hose = model.tube("ArmHoseL", [P(20.5, 83.8, -5.45), P(23.0, 83.3, -5.7), P(25.6, 79.0, -6.8),
                                   P(27.2, 71.0, -7.7), P(27.8, 62.0, -7.4), P(27.5, 55.0, -6.0),
                                   P(26.8, 49.0, -3.6)], 2.6*u)
# The first band starts clear of the shoulder-cup lip (>0.25 mm) where the hose turns down.
arm_stations = [((0, 0.6), (1, 0.525)), ((1, 0.635), (2, 0.233)), ((2, 0.31), (2, 0.765)),
                ((2, 0.833), (3, 0.273)), ((3, 0.338), (3, 0.757)), ((3, 0.825), (4, 0.315)),
                ((4, 0.398), (4, 0.965)), ((5, 0.052), (5, 0.9))]
arm_bands = []
for i, (a, b) in enumerate(arm_stations):
    pa = model.path_point(arm_hose, segment=a[0], fraction=a[1])
    pb = model.path_point(arm_hose, segment=b[0], fraction=b[1])
    arm_bands.append(model.tube("ArmBandL" + str(i), [pa, pb], 3.0*u))
cuff = model.sectioned_solid("HandCuffL", [(43.8*u, 4.4*u, 3.8*u), (49.8*u, 3.5*u, 3.2*u)], center_xz=(26.6*u, -3.2*u))
hand_parts = [arm_peg, arm_hose, cuff]
for i, dx in enumerate([-2.6, 0.0, 2.6]):
    top = P(26.6 + dx, 44.6, -2.8)
    tip = P(26.6 + 1.25*dx, 38.8, -2.0)
    hand_parts.append(model.tube("FingerL" + str(i), [top, tip], 1.2*u))
    hand_parts.append(model.ellipsoid("FingerTipL" + str(i), (1.2*u, 1.2*u, 1.2*u), center=tip))
arm_l = model.combine("ArmL", hand_parts + arm_bands)
arm_r = model.mirror("ArmR", [arm_l], plane="Right")

# ---------------- Legs (seed is the character's left leg, +X) ----------------
leg_top = (8.9*u, 46.5*u - Clr, 3.9*u)
leg_hose = model.tube("LegHoseL", [leg_top, P(8.9, 43.5, 3.9), P(10.9, 38.5, 3.6),
                                   P(12.5, 31.0, 3.5), P(13.0, 24.0, 3.2), P(12.8, 17.5, 2.3),
                                   P(12.2, 12.0, 0.8), P(12.0, 7.5, 0)], 3.2*u)
# The top band is vertical so its flat end is the seating face under the torso.
leg_bands = [model.tube("LegBandL0", [leg_top, P(8.9, 43.5, 3.9)], 3.6*u)]
leg_stations = [((1, 0.145), (1, 0.877)), ((1, 0.973), (2, 0.51)), ((2, 0.585), (3, 0.102)),
                ((3, 0.185), (3, 0.752)), ((3, 0.84), (4, 0.432)), ((4, 0.522), (5, 0.155)),
                ((5, 0.255), (5, 0.963)), ((6, 0.08), (6, 0.963))]
for i, (a, b) in enumerate(leg_stations):
    pa = model.path_point(leg_hose, segment=a[0], fraction=a[1])
    pb = model.path_point(leg_hose, segment=b[0], fraction=b[1])
    leg_bands.append(model.tube("LegBandL" + str(i + 1), [pa, pb], 3.6*u))
foot = model.dome("FootL", radius=11.0*u, height=9.6*u, center=P(12.0, 0, 0))
leg_peg = model.tube("LegPegL", [P(8.9, 45.5, 3.9), (8.9*u, 51.5*u - Clr, 3.9*u)], 2.4*u)
leg_l = model.combine("LegL", [leg_hose, foot, leg_peg] + leg_bands)
leg_r = model.mirror("LegR", [leg_l], plane="Right")
