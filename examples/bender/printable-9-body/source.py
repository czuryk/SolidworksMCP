H = model.parameter('H', 150)
Clr = model.parameter('Clr', 0.25)
u = H / 150

def P(x, y, z):
    return (x*u, y*u, z*u)

# ---------------- Torso: tapered elliptical wall, shoulder cone, raised door ----------------
YB, YW, YN = 48.2, 91.0, 98.2          # bottom, top of the wall, neck seam
core = model.sectioned_solid('TorsoCore', [(YB*u, 15.05*u, 13.35*u), (YW*u, 19.4*u, 15.95*u),
                                           (YN*u - Clr, 10.7*u, 10.7*u)], spans='linear')
body = model.fillet('TorsoBody', core, 1.2*u, edges={'planar_loop': {'axis': 'Y', 'side': 'min'}})
door_shell = model.sectioned_solid('DoorShell', [(YB*u, 15.65*u, 13.95*u), (YW*u, 20.0*u, 16.55*u)])
door_mask = model.prism('DoorMask', [(-11.6*u, 81.6*u), (11.5*u, 81.6*u), (13.1*u, 80.0*u), (11.1*u, 54.0*u),
                                     (8.8*u, 51.5*u), (-8.8*u, 51.5*u), (-11.2*u, 54.0*u), (-13.2*u, 80.0*u)],
                        14*u, center=(0, 0, 10*u))
door = model.combine('Door', [door_shell, door_mask], operation='intersection')
knob = model.ellipsoid('Knob', (1.3*u, 1.3*u, 1.3*u), center=P(9.4, 67.4, 12.8))
# Sockets: every mating face is one clearance away from its tab.
neck_slot = model.rounded_prism('NeckSlot', 9*u + 2*Clr, 7*u + 2*Clr, 7.5*u + Clr, 1.5*u + Clr,
                                center=(0, 95.95*u - Clr/2, 0), axis='Y')
SX, SY, SZ = 11.0, 83.8, -4.5          # floor of the shoulder bore and its axis
bore = model.tube('ArmBoreL', [P(SX, SY, SZ), P(25, SY, SZ)], 5.2*u + Clr)
arm_slot = model.rounded_prism('ArmSlotL', 5*u + 2*Clr, 4*u + 2*Clr, 6*u, 1.2*u + Clr,
                               center=P(SX - 2, SY, SZ), axis='X')
LX, LZ = 9.0, 2.3
leg_slot = model.rounded_prism('LegSlotL', 4.4*u + 2*Clr, 3.6*u + 2*Clr, 7.5*u + Clr, 1.1*u + Clr,
                               center=(LX*u, (YB + 2.25)*u + Clr/2, LZ*u), axis='Y')
cuts_r = model.mirror('CutsR', [bore, arm_slot, leg_slot])
solid = model.combine('TorsoSolid', [body, door, knob])
model.combine('Torso', [solid, neck_slot, bore, arm_slot, leg_slot, cuts_r], operation='difference')

# ---------------- Head: cylinder, dome, antenna, visor with one deep cavity ----------------
RH, YD, VY, TY = 10.7, 126.5, 116.7, 103.3
shell = model.revolve('HeadShell', [(YN*u, RH*u), (YD*u, RH*u)])
# Mouth: an annular sector with radial walls, so the recess meets the head surface squarely.
MS, MC = 0.8192, 0.5736                # sin and cos of the 55 degree half angle
teeth_ring = model.revolve('TeethRing', [((TY - 3.5)*u, 7.0*u), ((TY + 3.5)*u, 7.0*u), ((TY + 3.5)*u, 9.2*u),
                                         ((TY - 3.5)*u, 9.2*u)], closed=True)
teeth_sector = model.prism('TeethSector', [(0, 0), (14*MS*u, 14*MC*u), (0, 20*u), (-14*MS*u, 14*MC*u)], 10*u,
                           center=P(0, TY, 0), axis='Y')
teeth_blank = model.combine('TeethBlank', [teeth_ring, teeth_sector], operation='intersection')
mouth_ring = model.revolve('MouthRing', [((TY - 3.5)*u - Clr, 7.0*u - Clr), ((TY + 3.5)*u + Clr, 7.0*u - Clr),
                                         ((TY + 3.5)*u + Clr, 12*u), ((TY - 3.5)*u - Clr, 12*u)], closed=True)
mouth_sector = model.prism('MouthSector', [(0, -Clr/MS), (14*MS*u + Clr*MC, 14*MC*u - Clr*MS), (0, 20*u),
                                           (-14*MS*u - Clr*MC, 14*MC*u - Clr*MS)], 12*u, center=P(0, TY, 0), axis='Y')
mouth = model.combine('Mouth', [mouth_ring, mouth_sector], operation='intersection')
dome = model.dome('HeadDome', radius=RH*u, height=RH*u, center=P(0, YD, 0))
ant_base = model.dome('AntBase', radius=4.8*u, height=2.9*u, center=P(0, 135.4, 0))
ant_stick = model.revolve('AntStick', [(137.6*u, 1.75*u), (147.0*u, 1.1*u)])
ant_ball = model.ellipsoid('AntBall', (1.9*u, 1.9*u, 1.9*u), center=P(0, 148.1, 0))
visor_block = model.rounded_prism('VisorBlock', 27.8*u, 11*u, 17.9*u, 4.7*u, center=P(0, VY, 6.95))
visor_front = model.fillet('VisorFront', visor_block, 1.0*u, edges={'planar_loop': {'axis': 'Z', 'side': 'max'}})
visor = model.fillet('Visor', visor_front, 2.0*u, edges={'planar_loop': {'axis': 'Z', 'side': 'min'}})
neck_tab = model.rounded_prism('NeckTab', 9*u, 7*u, 6.5*u, 1.5*u, center=P(0, 95.45, 0), axis='Y')
head_solid = model.combine('HeadSolid', [shell, dome, ant_base, ant_stick, ant_ball, visor, neck_tab])
cavity = model.rounded_prism('Cavity', 25.2*u, 8.4*u, 12*u, 3.4*u, center=P(0, VY, 12))
EX, ZF = 4.9, 6.0                      # eye axis and cavity floor
# Rectangular and below the eye axis: the eye fits one way only.
eye_slot = model.rounded_prism('EyeSlotL', 3.8*u + 2*Clr, 3.0*u + 2*Clr, 5*u, 0.9*u + Clr, center=P(EX, VY - 0.5, 4.5))
eye_slot_r = model.mirror('EyeSlotR', [eye_slot])
model.combine('Head', [head_solid, cavity, mouth, eye_slot, eye_slot_r], operation='difference')

# ---------------- Teeth insert with a grid of grooves ----------------
ring = model.revolve('GrooveRing', [((TY - 1.42)*u, 8.75*u), ((TY - 0.92)*u, 8.75*u), ((TY - 0.92)*u, 11.5*u),
                                    ((TY - 1.42)*u, 11.5*u)], closed=True)
ring_up = model.pattern('GrooveRingUp', [ring], count=2, step=(0, 2.34*u, 0))
grooves = []
for name, x, z in [('GrooveA', -5.49, 7.38), ('GrooveB', -2.89, 8.73), ('GrooveC', 0, 9.2),
                   ('GrooveD', 2.89, 8.73), ('GrooveE', 5.49, 7.38)]:
    grooves.append(model.rounded_prism(name, 0.5*u, 8*u, 3.5*u, 0, center=P(x, TY, z + 1.3)))
model.combine('Teeth', [teeth_blank, ring, ring_up] + grooves, operation='difference')

# ---------------- Eyes: convex bulbs seated on the cavity floor ----------------
# An ellipsoid bulb, wider than tall like the reference, cut flat where it sits on the cavity floor.
ball = model.ellipsoid('EyeBallL', (4.5*u, 3.75*u, 4.2*u), center=P(EX, VY, 8.45))
trim = model.rounded_prism('EyeTrimL', 12*u, 12*u, 6*u, 0, center=(EX*u, VY*u, ZF*u + Clr - 3*u))
bulb = model.combine('EyeBulbL', [ball, trim], operation='difference')
peg = model.rounded_prism('EyePegL', 3.8*u, 3.0*u, 4.5*u, 0.9*u, center=(EX*u, (VY - 0.5)*u, 4.25*u + Clr))
pupil = model.rounded_prism('PupilCutL', 2.2*u, 2.2*u, 2*u, 0, center=P(EX + 0.5, VY - 0.5, 12.7))
eye_solid = model.combine('EyeSolidL', [bulb, peg])
eye = model.combine('EyeL', [eye_solid, pupil], operation='difference')
model.mirror('EyeR', [eye])

# ---------------- Arms: shoulder cap, curved tube, hand with three fingers ----------------
cap = model.tube('CapL', [(SX*u + Clr, SY*u, SZ*u), P(18.6, SY, SZ)], 5.2*u)
cap_dome = model.dome('CapDomeL', radius=5.2*u, height=3.5*u, center=P(18.5, SY, SZ), axis='X')
arm_tube = model.tube('ArmTubeL', [P(18.5, SY, SZ), P(21.5, 80.9, -6.3), P(24.95, 75.5, -7.4), P(26.9, 69.8, -7.95),
                                   P(27.8, 63.5, -7.5), P(27.75, 56.5, -6.2), P(27.0, 50.5, -4.0),
                                   P(26.5, 45.5, -1.8)], 3.3*u)
HX, HZ = 26.4, -1.4
bell = model.revolve('HandBellL', [(40.8*u, 6.0*u), (49.8*u, 3.4*u), (52.3*u, 0.6*u)], center_xz=(HX*u, HZ*u))
ftube = model.tube('FingerTubeL', [P(HX, 41.8, HZ + 4.1), P(HX, 38.9, HZ + 4.1)], 1.8*u)
ftip = model.ellipsoid('FingerTipL', (1.8*u, 1.8*u, 1.8*u), center=P(HX, 38.9, HZ + 4.1))
finger = model.combine('FingerL', [ftube, ftip])
fingers = model.pattern('FingersL', [finger], count=3, axis='Y', center=P(HX, 0, HZ))
arm_tab = model.rounded_prism('ArmTabL', 5*u, 4*u, 5.5*u, 1.2*u, center=((SX - 2.25)*u + Clr, SY*u, SZ*u), axis='X')
arm = model.combine('ArmL', [cap, cap_dome, arm_tube, bell, finger, fingers, arm_tab])
model.mirror('ArmR', [arm])

# ---------------- Legs with feet ----------------
leg_tube = model.tube('LegTubeL', [(LX*u, YB*u - Clr, LZ*u), P(LX, 42.8, LZ), P(10.75, 37.0, 3.0), P(12.35, 30.5, 3.65),
                                   P(12.85, 24.0, 3.4), P(12.5, 16.6, 2.15), P(12.0, 9.0, 0.6)],
                      3.8*u)
# A smooth loft through the measured foot profile: flat on the ground, fuller than a sphere.
foot = model.sectioned_solid('FootL', [(0, 10.3*u, 10.3*u), (2.3*u, 10.95*u, 10.95*u), (5.5*u, 10.1*u, 10.1*u),
                                       (8.4*u, 8.0*u, 8.0*u), (10.6*u, 3.6*u, 3.6*u)], center_xz=(12.05*u, 0.15*u))
leg_tab = model.rounded_prism('LegTabL', 4.4*u, 3.6*u, 6.5*u + Clr, 1.1*u, center=(LX*u, (YB + 2.75)*u - Clr/2, LZ*u), axis='Y')
leg = model.combine('LegL', [leg_tube, foot, leg_tab])
model.mirror('LegR', [leg])
