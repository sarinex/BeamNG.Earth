-- Backend replacement needs terrain support, not clearance from every padded streaming box.
-- Sample the vehicle's oriented ground footprint against the real static collision surface.
local stopwatch = require('beamngEarth/stopwatch')
local now = stopwatch.clock()
local M = { STEP = 0.25, HEIGHT_TOL = 0.02, CACHE_MS = 500 }
local cached = nil
function M.invalidate() cached=nil end

local function finite(x) return type(x)=='number' and x==x and math.abs(x)<math.huge end

-- Pure apart from the injected height/ray samplers. Corners are the OOBB bottom points 0,3,4,7.
function M.probe(corners, height, ray, box)
  if not box then return false, 'waiting for the terrain collision area' end
  for i=1,4 do
    local p=corners[i]
    if not p or not finite(p.x) or not finite(p.y) or not finite(p.z) then return false, 'waiting for a valid vehicle footprint' end
  end
  local a,b,c,d = corners[1],corners[2],corners[3],corners[4]
  local function length(p,q) return math.sqrt((p.x-q.x)^2+(p.y-q.y)^2) end
  local nx = math.max(1, math.ceil(math.max(length(a,b),length(c,d))/M.STEP))
  local ny = math.max(1, math.ceil(math.max(length(a,c),length(b,d))/M.STEP))
  if (nx+1)*(ny+1)>4096 then return false, 'vehicle footprint is too large to verify safely' end
  for j=0,ny do
    local v=j/ny
    for i=0,nx do
      local u=i/nx
      local x=a.x*(1-u)*(1-v)+b.x*u*(1-v)+c.x*(1-u)*v+d.x*u*v
      local y=a.y*(1-u)*(1-v)+b.y*u*(1-v)+c.y*(1-u)*v+d.y*u*v
      local z=a.z*(1-u)*(1-v)+b.z*u*(1-v)+c.z*(1-u)*v+d.z*u*v
      if not finite(x) or not finite(y) or not finite(z) then return false, 'waiting for a valid vehicle footprint' end
      if x<box[1] or y<box[2] or x>box[3] or y>box[4] then
        return false, 'move fully onto the terrain collision area'
      end
      local ground=height(x,y)
      if not finite(ground) or math.abs(z-ground)>0.5 then return false, 'stop with the vehicle resting on terrain' end
      local from=z+0.75
      local distance=ray(x,y,from,2)
      if not finite(distance) or distance<0 or distance>=2 then return false, 'waiting for terrain collision to settle' end
      if math.abs(from-distance-ground)>M.HEIGHT_TOL then
        return false, 'move off raised curbs, decks or other collision surfaces onto terrain'
      end
    end
  end
  return true
end

function M.current(box, collisionRevision)
  local vehicle = getPlayerVehicle and getPlayerVehicle(0)
  if not vehicle then return true end -- free camera: no vehicle support can be removed
  local terrain = require('beamngEarth/terrain')
  local tb=terrain.get()
  local ray=rawget(_G,'castRayStatic')
  if not tb or not ray or not vehicle.getSpawnWorldOOBB then return false, 'waiting for terrain collision information' end
  local ok, result, reason = pcall(function()
    if vehicle.getVelocity and vehicle:getVelocity():length()>0.25 then return false, 'stop the vehicle to rebuild roads' end
    local oobb=vehicle:getSpawnWorldOOBB()
    local corners={oobb:getPoint(0),oobb:getPoint(3),oobb:getPoint(4),oobb:getPoint(7)}
    local position=tb:getPosition()
    if not box then
      local size=(terrain.SIZE-1)*terrain.SQUARE_SIZE
      box={position.x,position.y,position.x+size,position.y+size}
    end
    local stamp=now()
    -- Cache blocked probes only; a successful decision always uses fresh collision samples.
    local same=cached and cached.safe==false and cached.vehicle==vehicle and cached.terrain==tb and cached.revision==collisionRevision
      and cached.z==position.z and stamp-cached.at<M.CACHE_MS
    if same then
      for i=1,4 do
        local p,q=corners[i],cached.corners[i]
        if math.abs(p.x-q.x)>0.02 or math.abs(p.y-q.y)>0.02 or math.abs(p.z-q.z)>0.02 then same=false;break end
        if box[i]~=cached.box[i] then same=false;break end
      end
    end
    if same then return cached.safe,cached.reason end
    local safe,why=M.probe(corners,
      function(x,y) return tb:getHeight(vec3(x,y,0)) end,
      function(x,y,z,distance) return ray(vec3(x,y,z),vec3(0,0,-1),distance) end,box)
    local points={}
    for i,p in ipairs(corners) do points[i]={x=p.x,y=p.y,z=p.z} end
    cached={vehicle=vehicle,terrain=tb,revision=collisionRevision,z=position.z,at=stamp,corners=points,
      box={box[1],box[2],box[3],box[4]},safe=safe,reason=why}
    return safe,why
  end)
  if not ok then return false, 'waiting for reliable vehicle and terrain collision information' end
  return result,reason
end

return M
