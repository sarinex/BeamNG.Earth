-- luajit mod/tests/test_roadgeom_safety.lua
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local safety = require('beamngEarth/roadgeom_safety')

-- The oriented footprint from the reported narrow-street false positive.
local corners = {
  {x=26.878967,y=-50.122963,z=2.349779}, {x=28.996269,y=-50.016190,z=2.355188},
  {x=27.104141,y=-54.587257,z=2.334446}, {x=29.221443,y=-54.480484,z=2.339854}}
local box={-512,-512,510,510}
local function height(x,y) return 2.397+(y+52)*.004 end
local probes=0
local raised=0
local function ray(x,y,z,length) probes=probes+1; return z-height(x,y)-raised end
local oldLeftBox={21.6,-67.5,1.3,27.2,-41.3,3.4}
T.check(28.3746>=oldLeftBox[1]-3 and 28.3746<=oldLeftBox[4]+3, 'fixture reproduces old three-metre curb-box rejection')
T.check(safety.probe(corners,height,ray,box), 'parked car on terrain between nearby padded sidewalk boxes can switch')
T.check(probes>100 and probes<4096, 'oriented footprint is sampled densely with a bounded probe count')
raised=.15
local safe,why=safety.probe(corners,height,ray,box)
T.check(not safe and why:find('curbs'), 'actual raised sidewalk/curb support blocks replacement')
raised=.021
T.check(not safety.probe(corners,height,ray,box), 'collision above the height tolerance is not treated as terrain')
raised=.001
T.check(safety.probe(corners,height,ray,box), 'sub-millimetre terrain interpolation differences are tolerated')
raised=0
local function island(x,y,z,length)
  return z-height(x,y)-((x>27.9 and x<28.2) and .15 or 0)
end
T.check(not safety.probe(corners,height,island,box), 'small raised region inside the footprint is detected, not just its corners')
T.check(not safety.probe(corners,function() return -10 end,ray,box), 'bridge deck above terrain is blocked')
T.check(not safety.probe(corners,function() return 10 end,ray,box), 'tunnel floor below terrain is blocked')
T.check(not safety.probe(corners,height,function(x,y,z,len) return len end,box), 'missing collision fails closed')
T.check(not safety.probe(corners,height,function() return 0/0 end,box), 'nonfinite collision fails closed')
T.check(not safety.probe(corners,function() return 0/0 end,ray,box), 'nonfinite terrain fails closed')
T.check(not safety.probe(corners,height,ray,{28,-512,510,510}), 'vehicle partly outside physics ground is blocked')
T.check(not safety.probe(corners,height,ray,nil), 'missing terrain bounds are blocked')
local invalid={{x=0/0,y=0,z=0},corners[2],corners[3],corners[4]}
T.check(not safety.probe(invalid,height,ray,box), 'invalid vehicle footprint fails closed')

-- Engine adapters and blocked-probe caching, using the same API shape as BeamNG.
local points={[0]=corners[1],[3]=corners[2],[4]=corners[3],[7]=corners[4]}
local velocity=0
local vehicle={getSpawnWorldOOBB=function() return {getPoint=function(_,i) return points[i] end} end,
  getVelocity=function() return {length=function() return velocity end} end}
getPlayerVehicle=function() return vehicle end
local terrain={getPosition=function() return vec3(-512,-512,-64) end,
  getHeight=function(_,p) return height(p.x,p.y) end}
package.loaded['beamngEarth/terrain']={SIZE=512,SQUARE_SIZE=2,get=function() return terrain end}
castRayStatic=function(p,d,len) return ray(p.x,p.y,p.z,len) end
raised=.15; probes=0; safety.invalidate()
T.check(not safety.current(box,1) and probes>0, 'adapter detects actual raised collision')
local before=probes
T.check(not safety.current(box,1) and probes==before, 'blocked checks are cached while position and collision stay unchanged')
T.check(not safety.current(box,2) and probes>before, 'collision rebuild invalidates blocked cache')
raised=0; safety.invalidate(); probes=0
T.check(safety.current(box,2), 'adapter verifies the live-style narrow-road fixture')
before=probes
T.check(safety.current(box,2) and probes>before, 'successful decisions always use fresh collision samples')
raised=.15; safety.invalidate(); safety.current(box,2); before=probes
points[0]={x=corners[1].x+.1,y=corners[1].y,z=corners[1].z}
safety.current(box,2)
T.check(probes>before, 'vehicle motion invalidates cached rejection')
velocity=1; before=probes
T.check(not safety.current(box,2) and probes==before, 'moving vehicles do not run terrain probes')
velocity=0
vehicle.getSpawnWorldOOBB=function() error('unavailable') end
T.check(not safety.current(box,2), 'engine information failures block safely instead of breaking updates')
getPlayerVehicle=function() return nil end
T.check(safety.current(box,2), 'free camera with no vehicle needs no support hold')
T.done('test_roadgeom_safety')
