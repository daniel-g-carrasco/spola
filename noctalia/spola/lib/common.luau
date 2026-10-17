--!nonstrict
-- Shared by every Spola entry: the snapshot the service publishes (Spola's
-- status.json as data, and whether Spola is running), the glyph and colour
-- of each status, and the channel for actions (only the service runs gdbus).

local M = {}

M.PANEL = "daniel-g-carrasco/spola:panel"

-- Spola's statuses (the engine's names) as Tabler glyphs and palette roles.
M.GLYPHS = {
  ok = "cloud-check",
  syncing = "cloud-up",
  pending = "cloud",
  paused = "cloud-pause",
  needs_setup = "cloud-question",
  error = "cloud-x",
  needs_repair = "cloud-exclamation",
  needs_confirmation = "cloud-exclamation",
  conflicts = "cloud-exclamation",
  skipped = "cloud-exclamation",
  off = "cloud-off",
}
M.COLORS = {
  ok = "on_surface_variant",
  syncing = "primary",
  pending = "on_surface_variant",
  paused = "on_surface_variant",
  needs_setup = "tertiary",
  error = "error",
  needs_repair = "error",
  needs_confirmation = "tertiary",
  conflicts = "tertiary",
  skipped = "tertiary",
  off = "on_surface_variant",
}
M.NEEDS_YOU = { needs_setup = true, needs_repair = true, needs_confirmation = true, conflicts = true, skipped = true }

function M.snapshot()
  local raw = noctalia.state.get("spola.data")
  local data = nil
  if raw and raw ~= "" then
    data = noctalia.json.decode(raw)
  end
  local ok = type(data) == "table"
  return {
    running = ok and noctalia.state.get("spola.running") == true,   -- never "running" without data
    data = ok and data or nil,
  }
end

function M.glyph(status)
  return M.GLYPHS[status] or M.GLYPHS.off
end

function M.color(status)
  return M.COLORS[status] or M.COLORS.off
end

function M.statusText(status)
  return noctalia.tr("status." .. (status or "off"))
end

-- "42%", or "" when the progress is unknown.
function M.percent(progress)
  if progress and progress.fraction then
    return string.format("%d%%", math.floor(progress.fraction * 100 + 0.5))
  end
  return ""
end

-- "1.2 GB", "340 MB", "12 kB".
function M.human(bytes)
  local v = tonumber(bytes) or 0
  for _, unit in { "B", "kB", "MB", "GB", "TB" } do
    if v < 1000 or unit == "TB" then
      if unit == "B" then
        return string.format("%d B", v)
      end
      return string.format("%.1f %s", v, unit)
    end
    v = v / 1000
  end
  return ""
end

-- "3 min", "1 h 05 min", "40 s".
function M.duration(seconds)
  local s = math.floor(tonumber(seconds) or 0)
  if s < 60 then
    return `{s} s`
  elseif s < 3600 then
    return noctalia.tr("time.minutes", { n = s // 60 })
  end
  return noctalia.tr("time.hours", { h = s // 3600, m = string.format("%02d", (s % 3600) // 60) })
end

-- "14:20" today, "16/10 23:11" otherwise.
function M.when(t)
  if not t or t <= 0 then
    return ""
  end
  local a = os.date("*t", t)
  local n = os.date("*t")
  if a.year == n.year and a.yday == n.yday then
    return os.date("%H:%M", t)
  end
  return os.date("%d/%m %H:%M", t)
end

-- A folder's second line: what it is doing, or when it last synced.
function M.detail(pair)
  if pair.syncing then
    local p = pair.progress
    local parts = {}
    local pct = M.percent(p)
    if pct ~= "" then
      table.insert(parts, pct)
    end
    if p and p.total_bytes and p.total_bytes > 0 then
      table.insert(parts, `{M.human(p.bytes)} / {M.human(p.total_bytes)}`)
    end
    if p and p.eta_s and p.eta_s > 0 then
      table.insert(parts, M.duration(p.eta_s))
    end
    if #parts == 0 then
      return pair.stopping and noctalia.tr("detail.stopping") or noctalia.tr("detail.comparing")
    end
    return table.concat(parts, " · ")
  end
  if pair.status == "conflicts" and pair.conflicts > 0 then
    return noctalia.trp("detail.conflicts", pair.conflicts, { n = pair.conflicts })
  end
  if pair.status == "skipped" and pair.refused > 0 then
    return noctalia.trp("detail.refused", pair.refused, { n = pair.refused })
  end
  if pair.message and pair.message ~= "" and pair.message ~= M.statusText(pair.status) then
    return pair.message
  end
  if pair.status == "ok" and pair.last_sync then
    return noctalia.tr("detail.last_sync", { time = M.when(pair.last_sync) })
  end
  return ""
end

function M.pairById(data, id)
  if not data then
    return nil
  end
  for _, p in data.pairs or {} do
    if p.id == id then
      return p
    end
  end
  return nil
end

-- The overall status for the bar: "off" when Spola is not running.
function M.overall(s)
  if not s.running or not s.data then
    return "off"
  end
  return s.data.overall or "off"
end

-- Hand an action to the service: send("sync", "/home/me/work"), send("action", "sync-all").
function M.send(...)
  local seq = (noctalia.state.get("spola.seq") or 0) + 1
  noctalia.state.set("spola.seq", seq)
  noctalia.state.set("spola.cmd", noctalia.json.encode({ seq = seq, args = { ... } }))
end

function M.watch(fn)
  for _, key in { "spola.running", "spola.data" } do
    noctalia.state.watch(key, fn)
  end
end

return M
