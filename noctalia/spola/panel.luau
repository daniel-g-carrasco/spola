--!nonstrict
-- Spola panel: the overall status with Sync All and Pause, the space on
-- Koofr, then every folder with its status, progress and two buttons (sync
-- now, open in Spola; a folder waiting for you gets Review instead).

local common = require("./lib/common.luau")

local function header(s)
  local overall = common.overall(s)
  local d = s.data
  local title, detail
  if not s.installed then
    title = noctalia.tr("tooltip.not_installed")
    detail = noctalia.tr("panel.not_installed_detail")
  elseif not s.running or not d then
    title = noctalia.tr("tooltip.not_running")
    detail = noctalia.tr("panel.not_running_detail")
  elseif d.blocked == "not-signed-in" then
    title = noctalia.tr("tooltip.not_signed_in")
    detail = ""
  elseif d.paused_all then
    title = noctalia.tr("status.paused_all")
    detail = noctalia.tr("panel.paused_all_detail")
  else
    title = common.statusText(overall)
    local syncing = d.syncing and common.pairById(d, d.syncing) or nil
    detail = syncing and noctalia.tr("panel.syncing_now", { name = syncing.name, detail = common.detail(syncing) }) or ""
    if detail == "" and d.space then
      detail = noctalia.tr("panel.space", { used = common.human(d.space.used), total = common.human(d.space.total) })
    end
  end
  local buttons = {}
  if not s.installed then
    table.insert(buttons, ui.button({ text = noctalia.tr("panel.website"), variant = "outline", controlSize = "sm", onClick = function()
      noctalia.runAsync({ "xdg-open", "https://spola.neistar.com" })
    end }))
  elseif not s.running or not d then
    table.insert(buttons, ui.button({ text = noctalia.tr("panel.start"), variant = "primary", controlSize = "sm", onClick = function()
      common.send("launch")
    end }))
  else
    table.insert(buttons, ui.button({ glyph = "refresh", variant = "ghost", controlSize = "sm", tooltip = noctalia.tr("panel.sync_all"), onClick = function()
      common.send("action", "sync-all")
    end }))
    table.insert(buttons, ui.button({
      glyph = d.paused_all and "player-play" or "player-pause",
      variant = "ghost",
      controlSize = "sm",
      tooltip = d.paused_all and noctalia.tr("panel.resume_all") or noctalia.tr("panel.pause_all"),
      onClick = function()
        common.send("action", "pause-all")
      end,
    }))
  end
  return ui.row({ gap = 10, align = "center", padding = 12, fill = "surface_variant/0.4", radius = 10 }, {
    ui.glyph({ name = common.glyph(overall), size = 24, color = common.color(overall) }),
    ui.column({ gap = 2, flexGrow = 1 }, {
      ui.label({ text = title, fontSize = 15, fontWeight = "bold", color = common.color(overall) }),
      ui.label({ text = detail, fontSize = 12, color = "on_surface_variant", maxLines = 2 }),
    }),
    ui.row({ gap = 4 }, buttons),
  })
end

local function folderRow(p)
  local color = common.color(p.status)
  local detail = common.detail(p)
  local children = {
    ui.label({ text = p.name, fontSize = 13, fontWeight = "bold", color = p.paused and "on_surface_variant" or "on_surface" }),
    ui.label({ text = common.statusText(p.status) .. (detail ~= "" and (" · " .. detail) or ""), fontSize = 11, color = color, maxLines = 2 }),
  }
  if p.syncing and p.progress and p.progress.fraction then
    table.insert(children, ui.progress({ progress = p.progress.fraction }))
  end
  local buttons = {}
  if p.needs_you then
    table.insert(buttons, ui.button({ text = noctalia.tr("panel.review"), variant = "primary", controlSize = "sm", onClick = function()
      common.send("show", p.id)
    end }))
  else
    table.insert(buttons, ui.button({
      glyph = "refresh",
      variant = "ghost",
      controlSize = "sm",
      tooltip = noctalia.tr("panel.sync_now"),
      onClick = function()
        common.send("sync", p["local"])
      end,
    }))
    table.insert(buttons, ui.button({
      glyph = "external-link",
      variant = "ghost",
      controlSize = "sm",
      tooltip = noctalia.tr("panel.open"),
      onClick = function()
        common.send("show", p.id)
      end,
    }))
  end
  return ui.row({ key = "pair-" .. p.id, gap = 10, align = "center", padding = 8 }, {
    ui.glyph({ name = common.glyph(p.status), size = 18, color = color }),
    ui.column({ gap = 2, flexGrow = 1 }, children),
    ui.row({ gap = 2 }, buttons),
  })
end

local function render()
  local s = common.snapshot()
  local rows = {}
  if s.running and s.data then
    for _, p in s.data.pairs or {} do
      table.insert(rows, folderRow(p))
    end
    if #rows == 0 then
      table.insert(rows, ui.label({ text = noctalia.tr("panel.no_folders"), fontSize = 12, color = "on_surface_variant", padding = 8 }))
    end
  end
  panel.render(ui.column({ gap = 12, padding = 16 }, {
    ui.row({ align = "center", justify = "space_between" }, {
      ui.label({ text = "Spola", fontSize = 16, fontWeight = "bold", color = "primary", flexGrow = 1 }),
      ui.button({ glyph = "external-link", variant = "ghost", controlSize = "sm", tooltip = noctalia.tr("panel.open_spola"), onClick = function()
        if common.snapshot().running then
          common.send("show", "")
        else
          common.send("launch")
        end
      end }),
    }),
    header(s),
    ui.scroll({ flexGrow = 1, gap = 2 }, rows),
  }))
end

common.watch(render)

function onOpen(_context)
  render()
end
