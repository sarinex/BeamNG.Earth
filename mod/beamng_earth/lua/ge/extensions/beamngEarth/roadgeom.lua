-- Optional protocol v1. Keep desired, server-accepted and actually-loaded backends distinct.
local M = {}
local desired, active, accepted = 'legacy', 'legacy', 'legacy'
local supported, epoch, pending, phase = nil, nil, nil, nil
local text, elapsed, still = 'Legacy (Original)', 0, 0
local held, wasPaused, complete = false, false, false
local diagnostics = {}
local incomplete, serverKnown, requestIds = false, true, false
local serial, awaiting, lastLoaded, failure = 0, nil, 'legacy', nil

local function activeText()
  if not active then
    return serverKnown and ('Road geometry incomplete (server: ' .. tostring(accepted) .. ')')
      or 'Road geometry incomplete (server selection unconfirmed)'
  end
  local label = active == 'geos' and 'GEOS + Earcut (Experimental)' or 'Legacy (Original)'
  if active == 'geos' and (diagnostics.fallbacks or 0) > 0 then
    label = label .. ' (legacy fallback on ' .. tostring(diagnostics.fallbacks) .. ' tile operations)'
  end
  return label
end

local function release(ctx)
  if held then ctx.pause(wasPaused); ctx.roads.setHold(false); held = false end
end

local function markIncomplete(reason)
  active, incomplete, complete, still = nil, true, false, 0
  phase, pending, failure, text = 'incomplete', nil, reason, reason
end

function M.request(value)
  if value ~= desired then still = 0 end
  desired = value
  if supported == false and value == 'geos' then
    desired, pending = 'legacy', nil
    text = 'Legacy: this server does not support GEOS + Earcut'
    return false
  end
  -- A previously loaded backend is not proof of the server's current selection, especially
  -- after a timeout. Recovery always gets a new acknowledged generation, even for the same value.
  pending = (incomplete or not serverKnown or value ~= accepted or value ~= active) and value or nil
  if pending and phase == 'incomplete' then phase = nil end
  if pending then
    text = (incomplete and 'Recovery pending ' or 'Pending ') .. pending .. ': stop the vehicle to rebuild roads'
  elseif not phase then text = failure or activeText() end
  return true
end

function M.hello(cap, saved)
  local recovering = incomplete or phase ~= nil or awaiting ~= nil
  supported = type(cap) == 'table' and cap.version == 1
  requestIds = supported and cap.requestIds == true
  epoch = supported and cap.epoch or nil
  accepted = supported and cap.active or 'legacy'
  awaiting, phase, complete, serverKnown, failure = nil, nil, false, true, nil
  incomplete = supported and recovering or false
  if incomplete then active = nil else active, lastLoaded = accepted, accepted end
  text = activeText()
  still = 0
  diagnostics = {}
  M.request(saved or 'legacy')
  if not supported and saved == 'geos' then return 'legacy' end
end

function M.state(msg, retire)
  if not supported or not awaiting then return end
  if requestIds and msg.requestId ~= awaiting.id then return end
  if msg.requestId ~= nil and msg.requestId ~= awaiting.id then return end
  if msg.ok and msg.backend ~= awaiting.backend then return end
  local live = phase == 'request' and held
  awaiting = nil
  serverKnown = true
  if msg.backend then accepted = msg.backend end
  if msg.epoch then epoch = msg.epoch end
  if not live then
    -- An acknowledgement can arrive after a request timed out. Learn the server selection,
    -- but never retire/load geometry after releasing the stationary simulation hold.
    local queued = pending
    markIncomplete('Late server acknowledgement; select a generator to rebuild or restore ' .. lastLoaded)
    if queued then M.request(queued) end
    return
  end
  if not msg.ok then
    local reason = tostring(msg.error or 'Road geometry request rejected')
    if incomplete or active ~= accepted then
      markIncomplete(reason .. '; select a generator to retry or restore ' .. lastLoaded)
    else
      desired, pending, phase, failure, text = accepted, nil, nil, reason, reason
      return accepted
    end
    return
  end
  active, incomplete, failure = nil, true, nil
  phase, elapsed, complete = 'loading', 0, false
  diagnostics = {}
  retire()
  text = 'Rebuilding ' .. accepted .. ' roads and collision; simulation held'
end

function M.accept(msg)
  if incomplete and phase ~= 'loading' then return false end
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
    if not pending and not phase then text = activeText() end
  end
end

function M.cancel(pause, roads)
  if held then pause(wasPaused); roads.setHold(false) end
  if phase == 'request' or phase == 'loading' then active, incomplete = nil, true end
  held, phase, pending, still = false, nil, nil, 0
end

function M.progress(msg)
  local geometry = msg.roadGeometry
  if type(geometry) == 'table' then
    if geometry.epoch ~= epoch then return end
    if (geometry.failed or 0) > 0 then complete = false; return end
  end
  local r = msg.roads
  complete = type(r) == 'table' and r[2] and r[2] > 0 and r[1] >= r[2]
end

function M.update(dt, ctx)
  if held then ctx.roads.setHold(true) end
  if held and not phase and not pending then
    release(ctx)
  end
  if not ctx.connected then
    release(ctx)
    if phase == 'request' or phase == 'loading' or incomplete then
      markIncomplete('Road geometry incomplete; companion disconnected. Reconnect to retry or restore ' .. lastLoaded)
    else phase = nil end
    awaiting, serverKnown = nil, false
    if pending then text = 'Pending ' .. pending .. ': companion server disconnected' end
    return
  end
  if phase == 'incomplete' then release(ctx); return end
  if phase == 'request' or phase == 'loading' then
    elapsed = elapsed + dt
    if phase == 'loading' and complete and ctx.settled() then
      active, lastLoaded, phase, incomplete, failure = accepted, accepted, nil, false, nil
      pending = desired ~= active and desired or nil
      text = activeText()
      release(ctx)
    elseif elapsed > 120 then
      local loading = phase == 'loading'
      local queued = desired ~= (loading and accepted or awaiting.backend) and desired or nil
      if not loading then serverKnown = false end
      markIncomplete('Road geometry timed out; select a generator to retry or restore ' .. lastLoaded)
      release(ctx)
      if queued then M.request(queued) end
    end
    return
  end
  if not pending or not supported then return end
  if awaiting and not requestIds then
    text = 'Waiting for the previous server acknowledgement; reconnect the companion if it does not respond'
    return
  end
  if ctx.multiplayer then release(ctx); text = 'Pending ' .. pending .. ': leave the multiplayer session to rebuild safely'; return end
  still = ctx.speed <= 0.25 and (still + dt) or 0
  if still < 1 then text = 'Pending ' .. pending .. ': stop the vehicle to rebuild roads'; return end
  local safe, reason = ctx.safe, nil
  if type(safe) == 'function' then safe, reason = safe() end
  if not safe then text = 'Pending ' .. pending .. ': ' .. (reason or 'stop on terrain, clear of bridges, tunnels and curbs'); return end
  if not held then wasPaused, held = ctx.paused(), true; ctx.pause(true) end
  ctx.roads.setHold(true)
  phase, elapsed, failure, serverKnown = 'request', 0, nil, false
  serial = serial + 1
  awaiting = {id=serial, backend=pending}
  ctx.send({ type = 'road_geometry', version = 1, backend = pending, speed = ctx.speed, requestId = serial })
  text = 'Waiting for server to accept ' .. pending
end

function M.status()
  local display = text
  if pending or phase then display = activeText() .. '; ' .. text end
  return {active = active, desired = desired, accepted = accepted, pending = pending, phase = phase,
    supported = supported, epoch = epoch, text = display, diagnostics = diagnostics,
    incomplete = incomplete, serverKnown = serverKnown, lastLoaded = lastLoaded}
end

function M.needsUpdate() return pending ~= nil or phase == 'request' or phase == 'loading' or held end

return M
