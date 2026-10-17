# Bender, H = 150 mm: printing and glue assembly

Files in `delivery/`:

- `model.SLDPRT`: native parametric SolidWorks part. Global variable `H` is the overall height
  including the antenna, `Clr` the glue gap. The editable working copy is `bender_retest.SLDPRT`.
- `model.step`: the complete model, 9 solid bodies, mm.
- `stl/`: one STL per printable body: `Torso`, `Head`, `ArmL`, `ArmR`, `LegL`, `LegR`, `EyeL`,
  `EyeR`, `Teeth`.
- `manifest.json`: file hashes and the export validation report.

L and R are Bender's own left and right: his left arm is on your right when he faces you.

## Joints

Every mating face is 0.25 mm away from its counterpart: tab sides, tab ends and seating faces. The
gap is the parameter `Clr` and stays 0.25 mm when `H` changes. There are no hinges, threads or
snap fits. All tabs are rectangular, so each part fits in one orientation (or two that are
obviously different); the eye tabs sit below the eye axis and fit one way only.

| Part | Tab (mm) | Goes into | Push direction | Seats on |
|---|---|---|---|---|
| Head | 9 x 7, 6 long, under the head | slot in the top of the shoulder cone | straight down | top of the cone |
| ArmL, ArmR | 5 x 4, 5 long, behind the shoulder cap | round socket on the side of the torso, slot in its floor | sideways, towards the torso | flat floor of the socket |
| LegL, LegR | 4.4 x 3.6, 6 long, on top of the leg | slot in the bottom of the torso | straight up | flat bottom of the torso |
| EyeL, EyeR | 3.8 x 3, 4 long, behind the bulb | slot in the floor of the visor cavity | straight back | floor of the cavity |
| Teeth | the curved plate itself | mouth recess | straight back | floor of the recess |

## Order

1. Glue the teeth into the mouth recess. The plate stays 1.5 mm below the head surface.
2. Glue the eyes into the visor cavity, pupils outward and slightly down.
3. Glue the legs into the torso, feet pointing outward, and let the torso stand on both feet.
4. Glue the arms into the shoulder sockets, hands down.
5. Glue the head onto the torso, face towards the door.

## Suggested print orientation

| Part | Orientation | Notes |
|---|---|---|
| Torso | upright on its flat bottom | sockets need no support |
| Head | upright, neck tab down | supports under the neck face around the tab and under the visor |
| LegL, LegR | upright on the flat foot | tab points up, no support |
| ArmL, ArmR | lying on the back of the arm | light support under the hand |
| EyeL, EyeR | flat back down, tab pointing down | supports under the flat back; small part, print slowly |
| Teeth | standing on its lower edge | grid grooves are 0.45 mm deep |

## What was checked

- Overall height 150.000 mm; with `H = 120` it is 120.000 mm and every gap is still 0.25 mm.
- All 8 joints: closest distance 0.25 mm, no interference (native body-pair measurement).
- The assembled model stands on both feet: the centre of mass is 10.2 mm inside the footprint,
  tipping angle 7.9 degrees.
- Walls of 1.2 mm or more everywhere; the thinnest is the visor rim at 1.28 mm. The back rim of
  each shoulder socket ends in a blunt edge of about 40 degrees.
- Silhouette against the approved views at 150 mm: front IoU 0.95, side IoU 0.94; all 48 height
  levels within 2 mm in both views.
- Export: all nine STL files are closed and consistently oriented; STEP holds nine closed shells.

## Differences from the references

- The two approved views disagree in places: the torso bottom is at 46.3 mm in the front view and
  49.8 mm in the side view (model: 48.2); the visor centre is at 116.0 and 117.4 mm (model: 116.7);
  the mouth centre at 102.4 and 104.2 mm (model: 103.3); the head radius is 10.95 and 10.35 mm
  (model: 10.7).
- The slanted brow lids over the eyes are not modelled: a lid fixed to the head would block the
  eyes from being inserted.
- The rings of the arms and legs are not grooved; the limbs are smooth tubes.
