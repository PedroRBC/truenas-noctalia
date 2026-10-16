--!nonstrict
local F = require("./format.luau")
local snapshot = noctalia.state.get("truenas_snapshot") or { data = {}, loading = true }
local activeTab = "overview"
local search = ""
local filter = 0
local favoritesOnly = false
local confirmation = nil
local feedback = nil
local sequence = 0
local dirty = true
local render

local function tr(key, values) return noctalia.tr(key, values) end
local function config(key, fallback)
  local value = noctalia.getConfig(key)
  if value == nil then return fallback end
  return value
end
local function label(text, extra)
  local props = { text = tostring(text), color = "on_surface_variant", fontSize = 12 }
  for key, value in pairs(extra or {}) do props[key] = value end
  return ui.label(props)
end
local function send(action, app, confirmed)
  sequence += 1
  noctalia.state.set("truenas_request", { action = action, app = app, confirmed = confirmed == true,
    requestId = "panel-" .. tostring(noctalia.nowMs()) .. "-" .. tostring(sequence) })
end
local function openUrl(url)
  if F.url(url) then noctalia.runAsync({ "xdg-open", url }) end
end
local function ready()
  return snapshot.connected == true and snapshot.stale ~= true and snapshot.busy ~= true
    and snapshot.refreshing ~= true and snapshot.inventory_fresh == true
    and snapshot.pending == nil and config("read_only", false) ~= true
end
local function ageText()
  local stamp = tonumber(snapshot.updated_at) or 0
  if stamp == 0 then return tr("status.no_data") end
  return tr("status.updated", { time = noctalia.formatTime("%H:%M:%S", stamp) })
end
local function meter(value)
  local number = F.number(value)
  return ui.progress({ progress = number and math.clamp(number / 100, 0, 1) or 0,
    height = 5, radius = 3, fill = "primary", track = "on_surface/0.10" })
end
local function statCard(title, glyph, value, detail, percent)
  local children = {
    ui.row({ gap = 7, align = "center" }, {
      ui.glyph({ name = glyph, size = 16, color = "primary" }), label(title),
    }),
    label(value, { fontSize = 30, fontWeight = "bold", color = "on_surface" }),
    label(detail, { maxLines = 1 }),
  }
  if percent ~= nil then table.insert(children, meter(percent)) end
  return ui.column({ flexGrow = 1, gap = 10, padding = 16, radius = 14, fill = "surface_variant/0.45" }, children)
end
local function infoRow(glyph, title, value)
  return ui.row({ gap = 10, align = "center" }, {
    ui.glyph({ name = glyph, size = 17, color = "on_surface_variant" }),
    label(title, { flexGrow = 1 }), label(value, { color = "on_surface", fontWeight = "bold" }),
  })
end
local function empty(glyph, title, text)
  return ui.column({ gap = 12, padding = 28, flexGrow = 1, align = "center", justify = "center" }, {
    ui.glyph({ name = glyph, size = 34, color = "on_surface_variant" }),
    label(title, { fontSize = 17, fontWeight = "bold", color = "on_surface" }),
    label(text, { maxLines = 3, textAlign = "center" }),
  })
end

local function sectionStatus(section)
  return (snapshot.inventory_sections or {})[section]
    or { available = type((snapshot.data or {})[section]) == "table", stale = true }
end

local function unavailable(section)
  return empty("alert-triangle", tr("status.section_unavailable", { section = tr("tabs." .. (section == "pools" and "storage" or section)) }),
    tr("status.refresh_help"))
end

local function overview()
  local data = snapshot.data or {}
  local m = data.metrics or {}
  local server = data.server or {}
  local apps = data.apps or {}
  local cpu = F.percent(m.cpu_percent)
  local memory = F.percent(m.memory_percent)
  local temp = F.number(m.temperature_c)
  local rows = {
    ui.row({ gap = 12 }, {
      statCard(tr("metrics.cpu"), "cpu", cpu, tr("metrics.cpu_help"), m.cpu_percent),
      statCard(tr("metrics.memory"), "database", memory,
        F.bytes(m.memory_used) .. " / " .. F.bytes(m.memory_total or server.total_memory), m.memory_percent),
    }),
  }
  local secondary = {}
  if config("show_temperature", true) ~= false then
    table.insert(secondary, statCard(tr("metrics.temperature"), "temperature", temp and string.format("%.0f °C", temp) or "—", tr("metrics.temperature_help")))
  end
  if config("show_arc", true) ~= false then
    table.insert(secondary, statCard(tr("metrics.arc"), "stack-2", F.bytes(m.arc_bytes), tr("metrics.arc_help")))
  end
  if #secondary > 0 then table.insert(rows, ui.row({ gap = 12 }, secondary)) end
  if config("show_network", true) ~= false then
    table.insert(rows, ui.column({ gap = 12, padding = 16, radius = 14, fill = "surface_variant/0.30" }, {
      ui.row({ align = "center", gap = 8 }, {
        ui.glyph({ name = "network", size = 17, color = "primary" }),
        label(tr("metrics.network"), { flexGrow = 1, color = "on_surface", fontWeight = "bold" }),
        label(m.network_interface or config("network_interface", "br1")),
      }),
      ui.row({ gap = 24 }, {
        label("↓ " .. F.rate(snapshot.rx_rate), { color = "on_surface", fontSize = 19, flexGrow = 1 }),
        label("↑ " .. F.rate(snapshot.tx_rate), { color = "on_surface", fontSize = 19, flexGrow = 1 }),
      }),
      label(tr("metrics.network_help"), { fontSize = 11 }),
    }))
  end
  table.insert(rows, ui.column({ gap = 14, padding = 14 }, {
    infoRow("clock", tr("metrics.uptime"), F.uptime(server.uptime_seconds)),
    infoRow("apps", tr("tabs.apps"), sectionStatus("apps").available
      and tostring(F.running(apps)) .. " / " .. tostring(#apps) .. " " .. tr("apps.running") or "—"),
    infoRow("server", tr("metrics.version"), server.version or "—"),
  }))
  return ui.scroll({ key = "overview-scroll", flexGrow = 1, gap = 12 }, rows)
end

local function requestAction(action, app)
  if not ready() then return end
  feedback = nil
  if action == "stop" and config("confirm_stop", true) ~= false
    or action == "restart" and config("confirm_restart", true) ~= false then
    confirmation = { action = action, app = app.name, target = snapshot.target }
  else
    send(action, app.name, false)
  end
  render()
end

local function appRow(app, favorite)
  local running = app.state == "RUNNING"
  local stopped = app.state == "STOPPED"
  local controls = {
    ui.glyph({ name = favorite and "star" or "cube", size = 19, color = favorite and "primary" or "on_surface_variant" }),
    ui.column({ gap = 4, flexGrow = 1 }, {
      label(app.name, { fontSize = 14, fontWeight = "bold", color = "on_surface", maxLines = 1 }),
      label(tostring(app.state or "UNKNOWN") .. (app.version and "  ·  " .. tostring(app.version) or "")
        .. (app.update_available and "  ·  " .. tr("apps.update") or ""), { fontSize = 11, maxLines = 1 }),
    }),
  }
  if F.url(app.portal) then
    table.insert(controls, ui.button({ key = app.name .. "-portal", glyph = "external-link", variant = "ghost",
      tooltip = tr("actions.open_app"), onClick = function() openUrl(app.portal) end }))
  end
  table.insert(controls, ui.button({ key = app.name .. "-start", glyph = "player-play", variant = "ghost",
    tooltip = tr("actions.start"), enabled = ready() and stopped,
    onClick = function() requestAction("start", app) end }))
  table.insert(controls, ui.button({ key = app.name .. "-stop", glyph = "player-stop", variant = "ghost",
    tooltip = tr("actions.stop"), enabled = ready() and running,
    onClick = function() requestAction("stop", app) end }))
  table.insert(controls, ui.button({ key = app.name .. "-restart", glyph = "refresh", variant = "ghost",
    tooltip = tr("actions.restart"), enabled = ready() and running,
    onClick = function() requestAction("restart", app) end }))
  return ui.row({ key = "app-" .. app.name, gap = 10, align = "center", paddingH = 12, paddingV = 10,
    fill = "surface_variant/0.30", radius = 10 }, controls)
end

local function applications()
  if not sectionStatus("apps").available then return unavailable("apps") end
  local favorite = F.csv(config("favorites", ""))
  local hidden = F.csv(config("hidden_apps", ""))
  local apps = {}
  for _, app in ipairs((snapshot.data or {}).apps or {}) do
    local matches = search == "" or tostring(app.name):lower():find(search:lower(), 1, true) ~= nil
    if matches and not hidden[app.name] and (not favoritesOnly or favorite[app.name])
      and (filter == 0 or filter == 1 and app.state == "RUNNING" or filter == 2 and app.state ~= "RUNNING") then
      table.insert(apps, app)
    end
  end
  table.sort(apps, function(a, b)
    if favorite[a.name] ~= favorite[b.name] then return favorite[a.name] == true end
    return a.name < b.name
  end)
  local rows = {}
  for _, app in ipairs(apps) do table.insert(rows, appRow(app, favorite[app.name] == true)) end
  if #rows == 0 then table.insert(rows, empty("search", tr("apps.empty"), tr("apps.empty_help"))) end
  return ui.column({ flexGrow = 1, gap = 12, align = "stretch" }, {
    ui.row({ gap = 8, align = "center" }, {
      ui.input({ key = "app-search", value = search, placeholder = tr("apps.search"), flexGrow = 1,
        onChange = function(value) search = tostring(value) dirty = true end }),
      ui.select({ key = "app-filter", width = 150, options = { tr("apps.all"), tr("apps.running"), tr("apps.other") },
        selectedIndex = filter, onChange = function(index) filter = tonumber(index) or 0 dirty = true end }),
      ui.button({ glyph = "star", selected = favoritesOnly, variant = favoritesOnly and "primary" or "outline",
        tooltip = tr("apps.favorites"), onClick = function() favoritesOnly = not favoritesOnly render() end }),
    }),
    label(tr("apps.help"), { fontSize = 11 }),
    ui.scroll({ key = "apps-scroll", flexGrow = 1, gap = 6, align = "stretch" }, rows),
  })
end

local function storage()
  if not sectionStatus("pools").available then return unavailable("pools") end
  local rows = {}
  for _, pool in ipairs((snapshot.data or {}).pools or {}) do
    local percent = F.number(pool.percent)
    if percent == nil and F.number(pool.size) and pool.size > 0 and F.number(pool.allocated) then
      percent = pool.allocated / pool.size * 100
    end
    table.insert(rows, ui.column({ key = "pool-" .. pool.name, gap = 14, padding = 18,
      radius = 14, fill = "surface_variant/0.40" }, {
      ui.row({ gap = 10, align = "center" }, {
        ui.glyph({ name = "database", size = 24, color = "primary" }),
        label(pool.name, { color = "on_surface", fontSize = 19, fontWeight = "bold", flexGrow = 1 }),
        ui.glyph({ name = pool.healthy == true and "circle-check" or "alert-triangle", size = 16,
          color = pool.healthy == true and "on_surface_variant" or "error" }),
        label(pool.status or "UNKNOWN", { color = "on_surface", fontWeight = "bold" }),
      }),
      meter(percent),
      ui.row({ gap = 10 }, {
        label(tr("storage.used", { value = F.bytes(pool.allocated) }), { flexGrow = 1 }),
        label(F.percent(percent), { color = "on_surface", fontWeight = "bold" }),
      }),
      infoRow("database", tr("storage.available"), F.bytes(pool.free)),
      infoRow("stack-2", tr("storage.total"), F.bytes(pool.size)),
    }))
  end
  if #rows == 0 then return empty("database", tr("storage.empty"), tr("status.refresh_help")) end
  table.insert(rows, label(tr("storage.help"), { maxLines = 2 }))
  return ui.scroll({ key = "storage-scroll", flexGrow = 1, gap = 12 }, rows)
end

local function alerts()
  if not sectionStatus("alerts").available then return unavailable("alerts") end
  local rows = {}
  for index, alert in ipairs((snapshot.data or {}).alerts or {}) do
    local critical = alert.level == "CRITICAL" or alert.level == "ERROR" or alert.level == "EMERGENCY" or alert.level == "ALERT"
    table.insert(rows, ui.row({ key = "alert-" .. tostring(alert.id or index), gap = 12,
      padding = 16, radius = 12, fill = "surface_variant/0.35" }, {
      ui.glyph({ name = critical and "alert-triangle" or "info-circle", size = 20, color = critical and "error" or "on_surface_variant" }),
      ui.column({ gap = 8, flexGrow = 1 }, {
        label(alert.level or "INFO", { color = "on_surface", fontWeight = "bold" }),
        label(alert.message or "", { maxLines = 6 }),
      }),
    }))
  end
  if #rows == 0 then
    return empty("shield-check", tr(sectionStatus("alerts").stale and "alerts.cached_empty" or "alerts.empty"), tr("alerts.help"))
  end
  return ui.scroll({ key = "alerts-scroll", flexGrow = 1, gap = 10 }, rows)
end

local function confirmView()
  local action = confirmation.action
  return ui.column({ flexGrow = 1, gap = 18, padding = 24, justify = "center" }, {
    ui.glyph({ name = "alert-triangle", size = 36, color = "error" }),
    label(tr("confirm.title", { action = tr("actions." .. action), app = confirmation.app }),
      { fontSize = 24, fontWeight = "bold", color = "on_surface", maxLines = 2 }),
    label(confirmation.target or "", { fontSize = 14 }),
    label(tr(action == "restart" and "confirm.restart" or "confirm.stop"), { maxLines = 4, fontSize = 14 }),
    ui.row({ justify = "end", gap = 10 }, {
      ui.button({ text = tr("actions.cancel"), variant = "outline", onClick = function() confirmation = nil render() end }),
      ui.button({ text = tr("actions." .. action), glyph = action == "stop" and "player-stop" or "refresh",
        variant = "destructive", enabled = ready() and confirmation.target == snapshot.target,
        onClick = function()
          local intent = confirmation
          confirmation = nil
          if intent and intent.target == snapshot.target and ready() then send(intent.action, intent.app, true) end
          render()
        end }),
    }),
  })
end

render = function()
  dirty = false
  local data = snapshot.data or {}
  local server = data.server or {}
  local connected = snapshot.connected == true and snapshot.stale ~= true
  local connectionLabel = connected and "status.connected" or "status.offline"
  if snapshot.stale and (snapshot.updated_at or 0) > 0 then connectionLabel = "status.stale" end
  local notices = {}
  if snapshot.error and snapshot.error ~= "" then
    table.insert(notices, label(snapshot.error, { color = "error", maxLines = 3 }))
  elseif snapshot.loading then
    table.insert(notices, label(tr("status.loading")))
  end
  if snapshot.busy then table.insert(notices, label(tr("status.busy", { action = snapshot.operation or "" }), { maxLines = 2 })) end
  if snapshot.pending then
    table.insert(notices, ui.row({ gap = 10, align = "center" }, {
      label(tr("status.pending"), { color = "error", flexGrow = 1, maxLines = 3 }),
      ui.button({ text = tr("actions.reconcile"), variant = "outline", enabled = snapshot.busy ~= true,
        onClick = function() send("reconcile") end }),
    }))
  end
  if feedback then table.insert(notices, label(feedback.message, { color = feedback.ok and "on_surface" or "error", maxLines = 3 })) end
  local inventoryWarnings = {}
  for _, section in ipairs({ "apps", "pools", "alerts" }) do
    local status = sectionStatus(section)
    if status.stale then
      local title = tr("tabs." .. (section == "pools" and "storage" or section))
      table.insert(inventoryWarnings, tr(status.available and "status.section_stale" or "status.section_unavailable", { section = title }))
    end
  end
  if #inventoryWarnings > 0 then
    table.insert(notices, label(table.concat(inventoryWarnings, " · "), { maxLines = 3 }))
  end
  local warnings = data.errors or {}
  if #warnings > 0 then table.insert(notices, label(table.concat(warnings, " · "):sub(1, 400), { maxLines = 2 })) end
  if data.truncated then table.insert(notices, label(tr("status.truncated"), { maxLines = 2 })) end
  local tabs = {}
  for _, tab in ipairs({ "overview", "apps", "storage", "alerts" }) do
    local text = tr("tabs." .. tab)
    if tab == "apps" and data.apps then text ..= "  " .. tostring(#data.apps) end
    if tab == "alerts" and #(data.alerts or {}) > 0 then text ..= "  " .. tostring(#data.alerts) end
    table.insert(tabs, ui.button({ key = "tab-" .. tab, text = text,
      variant = activeTab == tab and "primary" or "ghost", selected = activeTab == tab,
      onClick = function() activeTab = tab confirmation = nil render() end }))
  end
  local content
  if confirmation then content = confirmView()
  elseif snapshot.updated_at == 0 and not snapshot.loading then
    content = empty("plug-off", tr("status.offline"), tr("status.offline_help"))
  elseif activeTab == "apps" then content = applications()
  elseif activeTab == "storage" then content = storage()
  elseif activeTab == "alerts" then content = alerts()
  else content = overview() end
  panel.render(ui.column({ flexGrow = 1, gap = 14, padding = 16, align = "stretch" }, {
    ui.row({ gap = 12, align = "center" }, {
      ui.column({ padding = 10, radius = 12, fill = "primary/0.12" }, {
        ui.glyph({ name = "server", size = 25, color = "primary" }),
      }),
      ui.column({ gap = 5, flexGrow = 1 }, {
        label(config("server_name", "TrueNAS"), { color = "on_surface", fontWeight = "bold", fontSize = 23 }),
        label((server.hostname or config("ssh_host", "")) .. "  ·  " .. tr(connectionLabel)),
      }),
      ui.button({ glyph = "external-link", variant = "ghost", tooltip = tr("actions.web"),
        enabled = F.url(config("web_url", "")), onClick = function() openUrl(config("web_url", "")) end }),
      ui.button({ glyph = "settings", variant = "ghost", tooltip = tr("actions.settings"), onClick = function() noctalia.openSettings() end }),
      ui.button({ glyph = "refresh", variant = "outline", tooltip = tr("actions.refresh"),
        enabled = snapshot.busy ~= true and snapshot.refreshing ~= true, onClick = function() send("refresh") end }),
      ui.button({ glyph = "x", variant = "ghost", tooltip = tr("actions.close"), onClick = function() panel.close() end }),
    }),
    ui.row({ gap = 4 }, tabs),
    ui.column({ gap = 6 }, notices),
    content,
    ui.row({ gap = 8, align = "center" }, {
      ui.glyph({ name = connected and "circle-check" or "clock", size = 13, color = "on_surface_variant" }),
      label(ageText(), { fontSize = 11, flexGrow = 1 }),
      label(config("read_only", false) == true and tr("status.read_only") or tr("status.ssh"), { fontSize = 11 }),
    }),
  }))
end

noctalia.state.watch("truenas_snapshot", function(value)
  if type(value) == "table" then
    snapshot = value
    if confirmation and confirmation.target ~= snapshot.target then confirmation = nil end
    dirty = true
  end
end)
noctalia.state.watch("truenas_result", function(value)
  if type(value) == "table" then feedback = value dirty = true end
end)
panel.setWantsSecondTicks(true)
function onOpen(_context) confirmation = nil send("refresh") render() end
function onClose() confirmation = nil end
function onConfigChanged() confirmation = nil dirty = true end
function update() if dirty then render() end end
function onIpc(event, payload)
  if event == "tab" and (payload == "overview" or payload == "apps" or payload == "storage" or payload == "alerts") then
    activeTab = payload
    confirmation = nil
    render()
  end
end
render()
