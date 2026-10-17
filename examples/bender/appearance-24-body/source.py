# Bender: native parametric character, millimetres, Y up and +Z forward.
Height = model.parameter("Height", 150)
u = Height / 150
HeadRadius = model.parameter("HeadRadius", 10.7)
r = HeadRadius * u

def pts(values):
    return [(x*u, y*u, z*u) for x, y, z in values]

# Tapered elliptical torso with a recessed conformal access door.
body = model.sectioned_solid("TorsoBlank", [(46.6*u,15*u,13.1*u),(90.1*u,18.8*u,15.5*u)], spans="linear")
door_mask0 = model.prism("DoorPocketMaskSharp", [(-10.9*u,-14.5*u),(10.9*u,-14.5*u),(13.0*u,14.5*u),(-13.0*u,14.5*u)], 25*u, center=(0,67*u,16*u))
door_mask = model.fillet("DoorPocketMask", door_mask0, 1.7*u)
door_pocket = model.surface_patch("DoorPocket", body, door_mask, 1.1*u, inward=True)
panel_mask0 = model.prism("DoorInsertMaskSharp", [(-10.55*u,-14.15*u),(10.55*u,-14.15*u),(12.65*u,14.15*u),(-12.65*u,14.15*u)],25*u, center=(0,67*u,16*u))
panel_mask = model.fillet("DoorInsertMask", panel_mask0, 1.4*u)
panel = model.surface_patch("AccessDoor", body, panel_mask, .70*u, offset=-.12*u, inward=True)
model.combine("Torso", [body,door_pocket], operation="difference")
shoulder_cap = model.sectioned_solid("ShoulderCone", [(90.15*u,18.8*u,15.5*u),(97.2*u,10.6*u,10.4*u)],spans="linear")
rim = model.sectioned_solid("ShoulderRim", [(89.75*u,18.85*u,15.55*u),(90.35*u,18.85*u,15.55*u)],spans="linear")
model.combine("ShoulderShell",[shoulder_cap,rim])
knob = model.tube("DoorButton", pts([(9.2,66.6,11.8),(9.2,66.6,13.3)]),1.35*u)
model.fillet("DoorButtonRounded",knob,.28*u,edges={"planar_loop":{"axis":"Z","side":"max"}})

# Cylindrical head and smooth cap.
head = model.revolve("HeadBlank",[(96.5*u,r),(127.2*u,r)])
cap = model.dome("HeadDome",radius=r,height=8.2*u,center=(0,127.05*u,0))
antenna_base = model.dome("AntennaBase",3.45*u,2.1*u,center=(0,135.0*u,0))
antenna = model.revolve("AntennaStem",[(136.4*u,1.25*u),(147.0*u,.65*u)])
ball = model.ellipsoid("AntennaTip",(1.7*u,1.7*u,1.7*u),center=(0,148.3*u,0))

# Mouth pocket wraps around the face; an inset curved tooth plate follows it.
mouth_mask = model.rounded_prism("MouthPocketMask",20*u,8.2*u,28*u,3.2*u,center=(0,102.6*u,10*u))
mouth_inner = model.revolve("MouthPocketCore",[(98*u,8.45*u),(107.2*u,8.45*u)])
mouth_tool = model.combine("MouthPocketTool",[mouth_mask,mouth_inner],operation="difference")
head_cut = model.combine("HeadCarved",[head,mouth_tool],operation="difference")
model.combine("Head",[head_cut,cap,antenna_base,antenna,ball])

teeth_ring = model.revolve("TeethRing",[(98.8*u,8.5*u),(106.4*u,8.5*u),(106.4*u,9.45*u),(98.8*u,9.45*u)],closed=True)
teeth_mask = model.rounded_prism("TeethMask",19.3*u,7.55*u,25*u,2.8*u,center=(0,102.6*u,9*u))
teeth_plate = model.combine("TeethPlate",[teeth_ring,teeth_mask],operation="intersection")
vcut = model.rounded_prism("ToothVerticalCut",.30*u,10*u,25*u,0,center=(-7.0*u,102.6*u,10*u))
vcuts = model.pattern("ToothVerticalCuts",[vcut],count=5,step=(3.5*u,0,0))
hcut = model.rounded_prism("ToothHorizontalCut",24*u,.28*u,25*u,0,center=(0,101.3*u,10*u))
hcuts = model.pattern("ToothHorizontalCuts",[hcut],count=2,step=(0,2.6*u,0))
grid_lines = model.combine("ToothGridLines",[vcut,vcuts,hcut,hcuts])
grid_ring = model.revolve("ToothGridRing",[(98.5*u,9.20*u),(106.7*u,9.20*u),(106.7*u,9.48*u),(98.5*u,9.48*u)],closed=True)
grid_raw = model.combine("ToothGridRaw",[grid_lines,grid_ring],operation="intersection")
grid_mask = model.rounded_prism("ToothGridMask",19.25*u,7.50*u,25*u,2.8*u,center=(0,102.6*u,9*u))
model.combine("ToothGrid",[grid_raw,grid_mask],operation="intersection")
back_ring = model.revolve("MouthDarkRing",[(98.45*u,8.46*u),(106.75*u,8.46*u),(106.75*u,8.60*u),(98.45*u,8.60*u)],closed=True)
back_mask = model.rounded_prism("MouthDarkMask",19.8*u,8.1*u,26*u,3*u,center=(0,102.6*u,10*u))
model.combine("MouthShadow",[back_ring,back_mask],operation="intersection")

# The visor is a true protruding rounded housing, with a deep dark opening.
visor_blank = model.rounded_prism("VisorBlank",27.1*u,12.1*u,12.4*u,5.2*u,center=(0,116.25*u,9.0*u))
visor = model.fillet("VisorRounded",visor_blank,.6*u,edges={"planar_loop":{"axis":"Z","side":"max"}})
opening = model.rounded_prism("VisorOpening",24.3*u,9.6*u,5*u,4.4*u,center=(0,116.15*u,15.15*u))
model.combine("VisorHousing",[visor,opening],operation="difference")
model.rounded_prism("VisorShadow",24.15*u,9.5*u,1.2*u,4.35*u,center=(0,116.15*u,12.4*u))
eye_l = model.ellipsoid("EyeLeftBlank",(4.85*u,4.35*u,2.05*u),center=(-5.0*u,115.85*u,13.2*u))
brow_l = model.prism("EyeLeftBrowCut",[(-11*u,122*u),(-.1*u,117.4*u),(-.1*u,125*u),(-11*u,125*u)],10*u,center=(0,0,14*u))
eye = model.combine("EyeLeft",[eye_l,brow_l],operation="difference")
model.mirror("EyeRight",[eye],plane="Right")
pupil = model.rounded_prism("PupilLeft",1.75*u,1.75*u,1*u,.12*u,center=(-5.75*u,115.35*u,15.0*u))
model.mirror("PupilRight",[pupil],plane="Right")

# Symmetrical shoulder sockets, gently curved arms, and shallow circumferential seams.
socket = model.ellipsoid("ShoulderLeft",(3.2*u,5.9*u,5.3*u),center=(19.5*u,82.7*u,-4.9*u))
model.mirror("ShoulderRight",[socket],plane="Right")
arm_path = pts([(20.4,82.3,-5.0),(25.0,73.0,-7.5),(27.4,61.0,-7.0),(26.8,49.2,-3.7)])
arm_shell = model.tube("ArmLeftOuter",arm_path,3.05*u)
arm_core = model.tube("ArmLeftCore",arm_path,2.70*u)
arm_cut = model.rounded_prism("ArmSeamCutter",22*u,.34*u,22*u,0,center=(26*u,55.2*u,-5*u))
arm_cuts = model.pattern("ArmSeamCutters",[arm_cut],count=5,step=(0,6.1*u,0))
arm_segments = model.combine("ArmLeftSegments",[arm_shell,arm_cut,arm_cuts],operation="difference",expected_bodies=6)
arm = model.combine("ArmLeft",[arm_segments,arm_core])
model.mirror("ArmRight",[arm],plane="Right")

# Three separate rounded fingers on each flared palm.
palm = model.revolve("PalmLeft",[(42.3*u,4.7*u),(42.65*u,4.75*u),(49.6*u,2.9*u)],center_xz=(26.8*u,-3.5*u))
f1 = model.tube("FingerLeftInner",pts([(23.6,43.4,-3.5),(22.1,38.6,-2.8)]),1.28*u)
f2 = model.tube("FingerLeftCenter",pts([(26.8,43.3,-3.5),(26.5,37.8,-2.6)]),1.3*u)
f3 = model.tube("FingerLeftOuter",pts([(29.8,43.4,-3.5),(30.6,38.5,-2.7)]),1.28*u)
f1r = model.fillet("FingerLeftInnerRound",f1,.65*u)
f2r = model.fillet("FingerLeftCenterRound",f2,.65*u)
f3r = model.fillet("FingerLeftOuterRound",f3,.65*u)
hand = model.combine("HandLeft",[palm,f1r,f2r,f3r])
model.mirror("HandRight",[hand],plane="Right")

# Slightly bowed legs, repeated recessed seams and broad flat-bottomed feet.
leg_path = pts([(7.7,48.2,0),(11.8,35.5,3.3),(13.1,22.3,3.2),(12.1,9.0,0)])
leg_outer = model.tube("LegLeftOuter",leg_path,3.4*u)
leg_core = model.tube("LegLeftCore",leg_path,3.03*u)
leg_cut = model.rounded_prism("LegSeamCutter",20*u,.34*u,22*u,0,center=(12*u,14.3*u,1.5*u))
leg_cuts = model.pattern("LegSeamCutters",[leg_cut],count=5,step=(0,6.6*u,0))
leg_segments = model.combine("LegLeftSegments",[leg_outer,leg_cut,leg_cuts],operation="difference",expected_bodies=6)
leg = model.combine("LegLeft",[leg_segments,leg_core])
model.mirror("LegRight",[leg],plane="Right")
foot = model.dome("FootLeft",10.7*u,10.2*u,center=(12.1*u,0,.55*u))
model.mirror("FootRight",[foot],plane="Right")

