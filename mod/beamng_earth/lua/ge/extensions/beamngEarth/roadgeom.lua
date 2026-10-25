-- Optional protocol v1. Keep desired, server-accepted and actually-loaded backends distinct.
local M = {}
local desired, active, accepted = 'legacy', 'legacy', 'legacy'
local supported, epoch, pending, phase = nil, nil, nil, nil
local text, elapsed, still = 'Legacy (Original)', 0, 0
local held, wasPaused, complete = false, false, false
local diagnostics = {}

function M.request(value)
  desired = value
  if supported == false and value == 'geos' then
    desired, pending = 'legacy', nil
    text = 'Legacy: this server does not support GEOS + Earcut'
    return false
  end
  pending = value ~= active and value or nil
  if pending then text = 'Pending ' .. pending .. ': stop the vehicle to rebuild roads' end
  return true
end

function M.hello(cap, saved)
  supported = type(cap) == 'table' and cap.version == 1
  epoch = supported and cap.epoch or nil
  accepted = supported and cap.active or 'legacy'
  active, phase, complete = accepted, nil, false
  text = active == 'geos' and 'GEOS + Earcut (Experimental)' or 'Legacy (Original)'
  still = 0
  diagnostics = {}
  M.request(saved or 'legacy')
  if not supported and saved == 'geos' then return 'legacy' end
end

function M.state(msg, retire)
  if not supported or not phase or phase ~= 'request' then return end
  if not msg.ok then
    desired, pending, phase = active, nil, nil
    text = tostring(msg.error or 'Road geometry request rejected')
    return active
  end
  epoch, accepted = msg.epoch, msg.backend
  phase, elapsed, complete = 'loading', 0, false
  diagnostics = {}
  retire()
  text = 'Rebuilding ' .. accepted .. ' roads and collision; simulation held'
end

function M.accept(msg)
  return epoch == nil or (msg.roadEpoch == epoch and msg.roadBackend == accepted)
end

function M.tile(msg)
  local d = msg.data and msg.data.roadGeometry
  if d then
    diagnostics.stream = d.stream
    for _, key in ipairs({'fallbacks', 'geosCorners', 'earcutTriangles', 'invalid'}) do
      diagnostics[key] = (diagnostics[key] or 0) + (d[key] or 0)
    end
    if d.reason then diagnostics.lastFallback = d.reason end
  end
end

function M.cancel(pause, roads)
  if held then pause(wasPaused); roads.setHold(false) end
  held, phase, pending, still = false, nil, nil, 0
end

function M.progress(msg)
  local geometry = msg.roadGeometry
  if type(geometry) == 'table' and (geometry.epoch ~= epoch or (geometry.failed or 0) > 0) then
    complete = false
    return
  end
  local r = msg.roads
  complete = type(r) == 'table' and r[2] and r[2] > 0 and r[1] >= r[2]
end

function M.update(dt, ctx)
  if held then ctx.roads.setHold(true) end
  if held and not phase and not pending then
    ctx.pause(wasPaused); ctx.roads.setHold(false); held = false
  end
  if not ctx.connected then
    if held then ctx.pause(wasPaused); ctx.roads.setHold(false); held = false end
    phase = nil
    if pending then text = 'Pending ' .. pending .. ': companion server disconnected' end
    return
  end
  if phase then
    elapsed = elapsed + dt
    if phase == 'loading' and complete and ctx.settled() then
      active, phase = accepted, nil
      pending = desired ~= active and desired or nil
      text = active == 'geos' and 'GEOS + Earcut (Experimental)' or 'Legacy (Original)'
      if held then ctx.pause(wasPaused); ctx.roads.setHold(false); held = false end
    elseif elapsed > 120 then
      -- Terrain remains present. Resume on failure; show incomplete state and retry explicitly on the next selection.
      phase, pending = nil, nil
      text = 'Road rebuild incomplete; check server connection and retry the generator setting'
      if held then ctx.pause(wasPaused); ctx.roads.setHold(false); held = false end
    end
    return
  end
  if not pending or not supported then return end
  if ctx.multiplayer then text = 'Pending ' .. pending .. ': leave the multiplayer session to rebuild safely'; return end
  local safe = ctx.safe
  if type(safe) == 'function' then safe = safe() end
  if not safe then text = 'Pending ' .. pending .. ': stop on terrain, clear of bridges, tunnels and curbs'; return end
  still = ctx.speed <= 0.25 and (still + dt) or 0
  if still < 1 then return end
  wasPaused, held = ctx.paused(), true
  ctx.pause(true)
  ctx.roads.setHold(true)
  phase, elapsed = 'request', 0
  ctx.send({ type = 'road_geometry', version = 1, backend = pending, speed = ctx.speed })
  text = 'Waiting for server to accept ' .. pending
end

function M.status()
  return {active = active, desired = desired, accepted = accepted, pending = pending, phase = phase,
    supported = supported, epoch = epoch, text = text, diagnostics = diagnostics}
end

function M.needsUpdate() return pending ~= nil or phase ~= nil or held end

return M
