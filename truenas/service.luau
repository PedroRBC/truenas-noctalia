--!nonstrict
-- One transport owner for all surfaces. No shell, credentials, or remote install.
local snapshot = {
  connected = false, loading = true, busy = false, error = "", updated_at = 0,
  inventory_at = 0, inventory_sections = {}, revision = 0, pending = nil, data = {}, rx_rate = nil, tx_rate = nil,
}
local inFlight = false
local nextPoll = 0
local nextInventory = 0
local failures = 0
local generation = 0
local previousNetwork = nil
local configSignature = ""
local refreshQueued = false
local actionIds = {}
local actionOrder = {}
local reconfigure = false
local refresh

local function cfg(key, fallback)
  local value = noctalia.getConfig(key)
  if value == nil then return fallback end
  return value
end

local function seconds(key, fallback, lo, hi)
  return math.clamp(tonumber(cfg(key, fallback)) or fallback, lo, hi)
end

local function now()
  return noctalia.nowMs() / 1000
end

local function signature()
  return table.concat({ tostring(cfg("ssh_host", "")), tostring(cfg("ssh_user", "")),
    tostring(cfg("ssh_port", 22)), tostring(cfg("ssh_identity", "")), tostring(cfg("network_interface", "")) }, "\n")
end

local function publish()
  snapshot.refreshing = inFlight
  snapshot.read_only = cfg("read_only", false) == true
  snapshot.stale = not snapshot.connected or snapshot.updated_at == 0
    or now() - snapshot.updated_at > seconds("stale_after", 45, 15, 900)
  for _, section in ipairs({ "apps", "pools", "alerts" }) do
    local status = snapshot.inventory_sections[section] or { updated_at = 0 }
    status.available = status.updated_at > 0
    status.stale = not status.available or snapshot.stale or status.error ~= nil
      or now() - status.updated_at > seconds("inventory_interval", 60, 15, 900) * 2
    snapshot.inventory_sections[section] = status
  end
  snapshot.inventory_fresh = not snapshot.inventory_sections.apps.stale
  snapshot.target = tostring(cfg("ssh_user", "root")) .. "@" .. tostring(cfg("ssh_host", ""))
  snapshot.revision += 1
  noctalia.state.set("truenas_snapshot", snapshot)
end

local function resetConnection()
  generation += 1
  configSignature = signature()
  snapshot.connected = false
  snapshot.updated_at = 0
  snapshot.inventory_at = 0
  snapshot.inventory_sections = {}
  snapshot.error = ""
  snapshot.data = {}
  snapshot.pending = nil
  snapshot.rx_rate = nil
  snapshot.tx_rate = nil
  previousNetwork = nil
  nextPoll = 0
  nextInventory = 0
  failures = 0
end

local function arguments(operation, command)
  local pluginDir = noctalia.pluginDir()
  local dataDir = noctalia.pluginDataDir()
  if not pluginDir or not dataDir then return nil end
  local timeout = operation == "start" or operation == "stop" or operation == "restart"
  timeout = timeout and seconds("action_timeout", 120, 30, 180) or seconds("request_timeout", 20, 10, 60)
  local args = {
    "python3", "-I", pluginDir .. "/scripts/truenas_bridge.py",
    "--host", tostring(cfg("ssh_host", "192.168.99.114")),
    "--user", tostring(cfg("ssh_user", "root")),
    "--port", tostring(cfg("ssh_port", 22)),
    "--identity", tostring(cfg("ssh_identity", "")),
    "--interface", tostring(cfg("network_interface", "br1")),
    "--timeout", tostring(timeout), "--state-dir", dataDir, operation,
  }
  if command and command.app then
    table.insert(args, "--app")
    table.insert(args, tostring(command.app))
  end
  if command and command.requestId then
    table.insert(args, "--request-id")
    table.insert(args, tostring(command.requestId))
  end
  return args, (timeout + 8) * 1000
end

local function run(operation, command, callback)
  local args, timeout = arguments(operation, command)
  if not args then
    callback({ ok = false, error = "Could not locate the plugin or its private data directory." })
    return
  end
  local accepted = noctalia.runAsync(args, function(result)
    local decoded = nil
    if result and not result.stdoutTruncated then
      local valid, value = pcall(noctalia.json.decode, result.stdout or "")
      if valid then decoded = value end
    end
    if type(decoded) ~= "table" then
      local detail = result and tostring(result.stderr or "") or ""
      detail = detail:sub(1, 280)
      callback({ ok = false, unknown = operation == "start" or operation == "stop" or operation == "restart",
        error = result and result.timedOut and "Transport timed out; a remote operation may still be running."
          or "The SSH bridge returned no valid response. " .. detail })
      return
    end
    callback(decoded)
  end, timeout)
  if not accepted then
    callback({ ok = false, error = "Could not launch Python. Check that python3 and ssh are installed." })
  end
end

local function updateNetwork(metrics)
  local stamp = tonumber(metrics.network_timestamp)
  local rx = tonumber(metrics.network_rx_bytes)
  local tx = tonumber(metrics.network_tx_bytes)
  snapshot.rx_rate = nil
  snapshot.tx_rate = nil
  if stamp and rx and tx then
    if previousNetwork and stamp > previousNetwork.time and metrics.network_interface == previousNetwork.interface then
      local delta = stamp - previousNetwork.time
      if rx >= previousNetwork.rx and tx >= previousNetwork.tx and delta < 900 then
        snapshot.rx_rate = (rx - previousNetwork.rx) / delta
        snapshot.tx_rate = (tx - previousNetwork.tx) / delta
      end
    end
    previousNetwork = { time = stamp, rx = rx, tx = tx, interface = metrics.network_interface }
  else
    previousNetwork = nil
  end
end

local function resultMessage(command, result)
  local message = tostring(result.error or "")
  if result.ok then
    message = command.action == "reconcile" and "The previous operation outcome has been resolved."
      or tostring(command.action) .. " · " .. tostring(command.app or "") .. " completed."
  elseif message == "" then
    message = "The operation could not be completed."
  end
  noctalia.state.set("truenas_result", { requestId = command.requestId, ok = result.ok == true,
    unknown = result.unknown == true, message = message, at = now() })
  if cfg("notify_actions", true) ~= false then
    if result.ok then noctalia.notify("TrueNAS", message) else noctalia.notifyError("TrueNAS", message) end
  end
end

local function applyData(result, inventory)
  if type(result.data) ~= "table" then return end
  for key, value in pairs(result.data) do
    if key ~= "truncated" or inventory then snapshot.data[key] = value end
  end
  if type(result.data.metrics) == "table" then updateNetwork(result.data.metrics) end
  for _, section in ipairs({ "apps", "pools", "alerts" }) do
    if type(result.data[section]) == "table" then
      snapshot.inventory_sections[section] = { updated_at = now() }
    elseif inventory then
      local status = snapshot.inventory_sections[section] or { updated_at = 0 }
      status.error = "Inventory query did not return this section."
      for _, warning in ipairs(result.data.errors or {}) do
        if type(warning) == "string" and warning:sub(1, #section + 1) == section .. ":" then
          status.error = warning:sub(1, 220)
          break
        end
      end
      snapshot.inventory_sections[section] = status
    end
  end
  if type(result.data.apps) == "table" then snapshot.inventory_at = now() end
end

refresh = function(force)
  if inFlight or snapshot.busy then
    refreshQueued = refreshQueued or force == true
    return
  end
  local at = now()
  if not force and at < nextPoll then return end
  local inventory = force or at >= nextInventory or snapshot.inventory_at == 0
  local operation = snapshot.pending and "reconcile" or (inventory and "inventory" or "snapshot")
  inFlight = true
  snapshot.loading = snapshot.updated_at == 0
  publish()
  local thisGeneration = generation
  run(operation, nil, function(result)
    inFlight = false
    snapshot.loading = false
    if thisGeneration ~= generation then
      nextPoll = 0
      publish()
      return
    end
    if type(result.pending) == "table" then
      snapshot.pending = result.pending
    elseif result.unknown ~= true then
      snapshot.pending = nil
    end
    if result.unknown == true and not snapshot.pending then
      snapshot.pending = { message = result.error or "An operation has an unknown outcome." }
    end
    if result.ok == true then
      if operation == "reconcile" then
        if result.pending == nil or result.pending == false then snapshot.pending = nil end
        snapshot.error = ""
        nextPoll = snapshot.pending and now() + 15 or 0
      else
        applyData(result, operation == "inventory")
        snapshot.connected = true
        snapshot.updated_at = now()
        snapshot.error = ""
        failures = 0
        nextPoll = now() + seconds("refresh_interval", 10, 5, 300)
        if inventory then nextInventory = now() + seconds("inventory_interval", 60, 15, 900) end
      end
    else
      if operation ~= "reconcile" then snapshot.connected = false end
      if operation == "inventory" then
        for _, section in ipairs({ "apps", "pools", "alerts" }) do
          local status = snapshot.inventory_sections[section] or { updated_at = 0 }
          status.error = tostring(result.error or "Inventory collection failed."):sub(1, 220)
          snapshot.inventory_sections[section] = status
        end
      end
      snapshot.error = tostring(result.error or "Could not reach the server."):sub(1, 600)
      failures = math.min(failures + 1, 5)
      nextPoll = now() + math.min(120, seconds("refresh_interval", 10, 5, 300) * 2 ^ (failures - 1))
    end
    if operation == "reconcile" and snapshot.pending == nil then
      resultMessage({ action = "reconcile" }, result)
    end
    if refreshQueued then
      refreshQueued = false
      nextPoll = 0
      nextInventory = 0
    end
    publish()
  end)
end

local function dispatch(command)
  if type(command) ~= "table" then return end
  if command.action == "refresh" then refresh(true) return end
  if command.action == "reconcile" then
    if not inFlight and not snapshot.busy then
      snapshot.pending = snapshot.pending or { message = "Checking the operation journal." }
      refresh(true)
    end
    return
  end
  if command.action ~= "start" and command.action ~= "stop" and command.action ~= "restart" then return end
  if type(command.requestId) ~= "string" or actionIds[command.requestId] then return end
  actionIds[command.requestId] = true
  table.insert(actionOrder, command.requestId)
  if #actionOrder > 128 then actionIds[table.remove(actionOrder, 1)] = nil end
  local function reject(message)
    resultMessage(command, { ok = false, error = message })
  end
  if snapshot.busy or inFlight then reject("A request is already running. Try again after it finishes.") return end
  if cfg("read_only", false) == true then reject("Read-only mode is enabled.") return end
  if snapshot.pending then reject("Resolve the previous operation before submitting another.") return end
  local appInventory = snapshot.inventory_sections.apps or {}
  if not snapshot.connected or now() - snapshot.updated_at > seconds("stale_after", 45, 15, 900)
    or snapshot.inventory_at == 0 or appInventory.error ~= nil
    or now() - snapshot.inventory_at > seconds("inventory_interval", 60, 15, 900) * 2 then
    reject("Refresh the server and app list before making changes.") return
  end
  if (command.action == "stop" and cfg("confirm_stop", true) ~= false
    or command.action == "restart" and cfg("confirm_restart", true) ~= false) and command.confirmed ~= true then
    reject("This operation requires confirmation.") return
  end
  local app = nil
  for _, candidate in ipairs(snapshot.data.apps or {}) do
    if candidate.name == command.app then app = candidate break end
  end
  if not app then reject("The application is no longer in the current inventory.") return end
  snapshot.busy = true
  snapshot.operation = command.action .. " · " .. tostring(command.app)
  publish()
  run(command.action, command, function(result)
    snapshot.busy = false
    snapshot.operation = nil
    if type(result.pending) == "table" then
      snapshot.pending = result.pending
    elseif result.unknown ~= true then
      snapshot.pending = nil
    end
    if result.unknown == true and not snapshot.pending then
      snapshot.pending = { message = "The result is unknown. Reconcile before another action." }
    end
    resultMessage(command, result)
    if reconfigure then
      reconfigure = false
      resetConnection()
    end
    nextPoll = 0
    nextInventory = 0
    refreshQueued = false
    publish()
    refresh(true)
  end)
end

noctalia.state.watch("truenas_request", dispatch)
noctalia.setUpdateInterval(1000)
resetConnection()
publish()
refresh(true)

function update()
  publish()
  refresh(false)
end

function onConfigChanged()
  if signature() ~= configSignature then
    if snapshot.busy then reconfigure = true else resetConnection() end
  end
  publish()
  refresh(true)
end

function onIpc(event, _payload)
  -- IPC deliberately exposes only read-only refresh/reconciliation, never mutations.
  if event == "refresh" then refresh(true) end
  if event == "reconcile" then dispatch({ action = "reconcile" }) end
end
