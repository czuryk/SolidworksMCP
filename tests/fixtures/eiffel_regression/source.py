# Eiffel Tower miniature, engineered for a 0.4 mm FDM nozzle.
# Units are millimetres; Y is vertical. All repeated sides use native patterns.
H = model.parameter('Height', 150)
B = model.parameter('BraceDiameter', 1.3)
R = model.parameter('MainRailDiameter', 1.8)
u = H / 150

def point(x, y, z):
    return (x*u, y*u, z*u)

def rod(name, a, b, diameter):
    return model.tube(name, [point(*a), point(*b)], diameter/2)

def box(name, w, d, t, x, y, z):
    return model.rounded_prism(name, w*u, d*u, t*u, 0, center=point(x,y,z), axis='Y')

def ring(name, outer, inner, y, thickness):
    outer_body = box(name+'Blank',outer,outer,thickness,0,y,0)
    opening = box(name+'Opening',inner,inner,thickness+2,0,y,0)
    return model.combine(name,[outer_body,opening],operation='difference')

def fourfold(name, seed):
    copies = model.pattern(name+'Copies',[seed],count=4,axis='Y')
    return [seed,copies]

# A complete four-chord corner leg, including its flat foundation foot.
stations = [(1.25,26.1,19.5),(8,22.2,16.0),(17,17.9,13.0),
            (25.9,14.7,10.9),(34.7,11.95,8.8),
            (43.5,10.1,7.3),(52.3,8.6,6.1)]
leg_parts = [box('FoundationFoot',11.2,11.2,1.8,22.8,0.9,22.8),
             box('FootPlinth',9.5,9.5,1.2,22.8,2.0,22.8)]
corners = [(0,0),(1,0),(1,1),(0,1)]
paths = []
for c,(ix,iz) in enumerate(corners):
    path = []
    for y,outer,inner in [stations[0],stations[3],stations[6]]:
        xs = [outer,inner]
        path.append(point(xs[ix],y,xs[iz]))
    paths.append(path)
    leg_parts.append(model.tube('LegChord'+str(c),path,R/2))
for j,(y,outer,inner) in enumerate(stations):
    pts = [(outer,y,outer),(inner,y,outer),(inner,y,inner),(outer,y,inner)]
    for side in [0,3]:
        leg_parts.append(rod('LegTie'+str(j)+'_'+str(side),pts[side],pts[(side+1)%4],B))
for j in range(len(stations)-1):
    ya,oa,ia = stations[j]
    yb,ob,ib = stations[j+1]
    aa = [(oa,ya,oa),(ia,ya,oa),(ia,ya,ia),(oa,ya,ia)]
    bb = [(ob,yb,ob),(ib,yb,ob),(ib,yb,ib),(ob,yb,ib)]
    for side in [0,3]:
        nxt = (side+1)%4
        leg_parts.append(rod('LegDiagonal'+str(j)+'_'+str(side),aa[side],bb[nxt],B))
        if side == 0 or side == 3:
            leg_parts.append(rod('LegCross'+str(j)+'_'+str(side),aa[nxt],bb[side],B))
leg = model.combine('LatticedLeg',leg_parts)
parts = fourfold('FourLegs',leg)

# Four open decorative entrance arches with spandrel uprights.
arch_points = [(-16,8,22.2),(-12,17.7,17.8),(-5,22.5,15.0),
               (5,22.5,15.0),(12,17.7,17.8),(16,8,22.2)]
arch_parts = [model.tube('EntranceArch',[point(*p) for p in arch_points],1.1*u)]
spandrel_points = [(-12,17.7,17.8),(-7,21.8,15.7),(0,23.1,14.7),
                   (7,21.8,15.7),(12,17.7,17.8)]
for j,(x,y,z) in enumerate(spandrel_points):
    arch_parts.append(rod('ArchSpandrel'+str(j),(x,y,z),(x,25.4,15.2),B))
arch = model.combine('ArchWithSpandrels',arch_parts)
parts += fourfold('FourArches',arch)

# Broad open terraces and double rims preserve the characteristic silhouette.
parts.append(ring('FirstDeck',34.0,20.0,25.9,1.6))
parts.append(ring('FirstCornice',34.6,29.8,25.0,1.3))
parts.append(ring('SecondDeck',21.0,9.0,52.3,1.6))
parts.append(ring('SecondCornice',21.6,17.2,51.4,1.3))

def balustrade(name,half,y,post_count,step):
    rail = rod(name+'TopRail',(-half,y+2.6,half),(half,y+2.6,half),B)
    bottom = rod(name+'BottomRail',(-half,y+0.5,half),(half,y+0.5,half),B)
    first_x = -(post_count-1)*step/2
    post = rod(name+'Post',(first_x,y+0.2,half),(first_x,y+2.6,half),B)
    posts = model.pattern(name+'PostRow',[post],count=post_count,step=point(step,0,0))
    side = model.combine(name+'Side',[rail,bottom,post,posts])
    return fourfold(name+'Sides',side)

parts += balustrade('FirstBalustrade',16.4,26.0,11,3.28)
parts += balustrade('SecondBalustrade',9.9,52.4,7,3.3)

# The upper pylon has eleven crossed bays on each of four faces.
upper = [(52.3,8.6),(59,7.5),(65,6.6),(71,5.9),(78,5.1),
         (85,4.6),(92,4.15),(99,3.8),(106,3.45),(113,3.15),
         (119,2.9),(125.5,2.7)]
upper_parts = []
upper_parts.append(model.tube('PylonRightChord',[point(w,y,w) for y,w in upper],0.8*u))
for j,(y,w) in enumerate(upper):
    upper_parts.append(rod('PylonHorizontal'+str(j),(-w,y,w),(w,y,w),B))
for j in range(len(upper)-1):
    ya,wa = upper[j]
    yb,wb = upper[j+1]
    upper_parts.append(rod('PylonDiagonalA'+str(j),(-wa,ya,wa),(wb,yb,wb),B))
    upper_parts.append(rod('PylonDiagonalB'+str(j),(wa,ya,wa),(-wb,yb,wb),B))
pylon_side = model.combine('UpperLatticeFace',upper_parts)
parts += fourfold('UpperLattice',pylon_side)

# Open summit cabin, observation ledge, roof and reinforced stepped antenna.
parts.append(box('SummitDeck',8.4,8.4,1.6,0,125.5,0))
summit_side = [rod('SummitLeftPost',(-2.7,125.5,2.7),(-2.7,130.3,2.7),B),
               rod('SummitCenterPost',(0,125.5,2.7),(0,130.3,2.7),B),
               rod('SummitWindowSill',(-2.7,127.4,2.7),(2.7,127.4,2.7),B),
               rod('SummitHeader',(-2.7,130.0,2.7),(2.7,130.0,2.7),B)]
summit_frame = model.combine('SummitWindowFrame',summit_side)
parts += fourfold('SummitFrames',summit_frame)
parts.append(box('SummitRoofSlab',7.2,7.2,1.3,0,130.5,0))
parts.append(model.dome('SummitRoof',4.1*u,1.8*u,center=point(0,130.6,0)))
parts.append(model.revolve('Antenna',[(131.8*u,2.0*u),(136*u,1.7*u),
             (140*u,1.2*u),(144*u,0.9*u),(147*u,0.65*u),(H,0.65*u)]))
# Add connected platforms before the separated patterned legs and faces.
lower_tower = model.combine('LowerTower',[parts[4],parts[0],parts[1],parts[5],parts[2],parts[3],parts[8],parts[9],parts[6],parts[7],parts[10],parts[11]],expected_bodies=1)
upper_tower = model.combine('UpperTower',[parts[14],parts[12],parts[13]],expected_bodies=1)
model.combine('EiffelTower150mm',[lower_tower,upper_tower]+parts[15:],expected_bodies=1)


