# Abstract nine-body fixture for scaling, draft and response-size regressions.
# Seven ribbed columns and two slotted brackets exercise 88 component nodes.
H = model.parameter("H", 150)
Clr = model.parameter("Clr", 0.25)
u = H / 150

for index in range(7):
    x = 28 * index * u
    core = model.sectioned_solid("Core" + str(index), [
        (0, 8*u, 8*u), (H/4, 10*u, 9*u), (H/2, 12*u, 10*u),
        (3*H/4, 10*u, 9*u), (H, 8*u, 8*u)], center_xz=(x, 0))
    ribs = []
    for station in range(10):
        ribs.append(model.tube("Rib" + str(index) + "_" + str(station),
            [(x + 8*u, (5 + 12*station)*u, 0),
             (x + 8*u, (15 + 12*station)*u, 0)], 2*u))
    model.combine("Column" + str(index), [core] + ribs)

block = model.rounded_prism("BracketBlankL", 12*u, 20*u, 8*u, 1*u,
                            center=(15*u, H/2, 30*u))
slot = model.rounded_prism("SlotCutL", 1.7*u + Clr, 1.7*u + Clr, 12*u, 0.15*u, center=(15*u, H/2, 30*u))
bracket = model.combine("BracketL", [block, slot], operation="difference")
model.mirror("BracketR", [bracket], plane="Right")
