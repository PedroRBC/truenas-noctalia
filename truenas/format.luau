--!strict
local Format = {}

function Format.number(value: any): number?
  local number = tonumber(value)
  if number == nil or number ~= number or math.abs(number) == math.huge then return nil end
  return number
end

function Format.percent(value: any): string
  local number = Format.number(value)
  if number == nil then return "—" end
  if number > 0 and number < 0.1 then return "<0.1%" end
  return string.format(number > 0 and number < 1 and "%.1f%%" or "%.0f%%", number)
end

function Format.bytes(value: any): string
  local number = Format.number(value)
  if number == nil or number < 0 then return "—" end
  local units = { "B", "KiB", "MiB", "GiB", "TiB", "PiB" }
  local index = 1
  while number >= 1024 and index < #units do number /= 1024 index += 1 end
  return string.format((number >= 100 or index == 1) and "%.0f %s" or "%.1f %s", number, units[index])
end

function Format.rate(value: any): string
  return Format.number(value) ~= nil and Format.bytes(value) .. "/s" or "—"
end

function Format.uptime(value: any): string
  local number = Format.number(value)
  if not number then return "—" end
  local days = math.floor(number / 86400)
  local hours = math.floor(number % 86400 / 3600)
  local minutes = math.floor(number % 3600 / 60)
  return days > 0 and string.format("%dd %dh", days, hours) or string.format("%dh %dm", hours, minutes)
end

function Format.csv(value: any): { [string]: boolean }
  local values = {}
  for item in tostring(value or ""):gmatch("[^,]+") do
    local trimmed = item:match("^%s*(.-)%s*$") or ""
    if trimmed ~= "" then values[trimmed] = true end
  end
  return values
end

function Format.running(apps: any): number
  local count = 0
  for _, app in ipairs(apps or {}) do if app.state == "RUNNING" then count += 1 end end
  return count
end

function Format.url(value: any): boolean
  return type(value) == "string" and #value <= 2048
    and value:match("^https?://[^/%s?#]+") ~= nil and value:find("[%c%s]") == nil
end

return Format
