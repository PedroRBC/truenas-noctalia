--!nonstrict
local F = require("./format.luau")
local snapshot = noctalia.state.get("truenas_snapshot") or { data = {}, loading = true }
local request = 0

local function render()
  local data = snapshot.data or {}
  local metrics = data.metrics or {}
  local apps = data.apps or {}
  local name = tostring(noctalia.getConfig("server_name") or "TrueNAS")
  local mode = noctalia.getConfig("bar_metric") or "apps"
  local online = snapshot.connected == true and snapshot.stale ~= true
  local warning = snapshot.pending ~= nil or #(data.alerts or {}) > 0
  local inventoryWarnings = {}
  for _, section in ipairs({ "apps", "pools", "alerts" }) do
    local status = (snapshot.inventory_sections or {})[section]
    if not status or status.stale then
      warning = true
      local title = noctalia.tr("tabs." .. (section == "pools" and "storage" or section))
      table.insert(inventoryWarnings, noctalia.tr(status and status.available and "status.section_stale" or "status.section_unavailable", { section = title }))
    end
  end
  local appCount = type(data.apps) == "table" and tostring(F.running(apps)) .. "/" .. tostring(#apps) or "—"
  for _, pool in ipairs(data.pools or {}) do if pool.healthy == false then warning = true end end
  local text = ""
  if mode == "apps" then text = appCount end
  if mode == "cpu" then text = F.percent(metrics.cpu_percent) end
  if mode == "memory" then text = F.percent(metrics.memory_percent) end
  if mode == "name" then text = name end
  if not online and mode ~= "icon" then text = snapshot.loading and "···" or "—" end
  local color = online and (noctalia.getConfig("bar_color") or "primary") or "on_surface_variant"
  local children = {
    ui.glyph({ name = "server", size = 17, color = color }),
  }
  if text ~= "" then table.insert(children, ui.label({ text = text, fontSize = 12, fontWeight = "bold" })) end
  if not online or warning or snapshot.busy then
    table.insert(children, ui.glyph({ name = snapshot.busy and "loader" or (online and "alert-circle" or "plug-off"),
      size = 12, color = warning and "error" or "on_surface_variant" }))
  end
  local container = barWidget.isVertical() and ui.column or ui.row
  barWidget.render(container({ gap = 5, align = "center" }, children))
  local state = online and noctalia.tr("status.connected") or noctalia.tr("status.offline")
  if snapshot.stale and snapshot.updated_at and snapshot.updated_at > 0 then state = noctalia.tr("status.stale") end
  barWidget.setTooltip(name .. " · " .. state .. "\n"
    .. "CPU " .. F.percent(metrics.cpu_percent) .. "  ·  RAM " .. F.percent(metrics.memory_percent) .. "\n"
    .. appCount .. " " .. noctalia.tr("apps.running")
    .. (#inventoryWarnings > 0 and "\n" .. table.concat(inventoryWarnings, " · ") or "")
    .. "\n" .. noctalia.tr("widget.hint"))
end

noctalia.state.watch("truenas_snapshot", function(value)
  if type(value) == "table" then snapshot = value render() end
end)
render()

function onClick() noctalia.togglePanel("PedroRBC/truenas:manager") end
function onRightClick()
  request += 1
  noctalia.state.set("truenas_request", { action = "refresh", requestId = "bar-" .. tostring(request) })
end
function onConfigChanged() render() end
