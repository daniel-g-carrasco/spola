--!nonstrict
-- Spola bar indicator: a cloud that says the overall status (lit while a
-- sync runs, warning colour when a folder waits for you, dim when every
-- folder is up to date or Spola is not running), with the progress or the
-- folder being synced as text. Click opens the panel; right click syncs
-- everything; middle click pauses or resumes.

local common = require("./lib/common.luau")

local function render()
  local s = common.snapshot()
  local overall = common.overall(s)
  local mode = noctalia.getConfig("show_text") or "progress"
  local text = ""
  local rows = {}
  if not s.running or not s.data then
    table.insert(rows, { "Spola", noctalia.tr("tooltip.not_running") })
  else
    local d = s.data
    local syncing = d.syncing and common.pairById(d, d.syncing) or nil
    local head = common.statusText(overall)
    if d.blocked == "not-signed-in" then
      head = noctalia.tr("tooltip.not_signed_in")
    elseif d.paused_all then
      head = noctalia.tr("status.paused_all")
    end
    table.insert(rows, { "Spola", head })
    if syncing then
      local pct = common.percent(syncing.progress)
      if mode == "progress" then
        text = pct
      elseif mode == "folder" then
        text = syncing.name
      end
    end
    local shown = 0
    for _, p in d.pairs or {} do
      if shown >= 8 then
        table.insert(rows, { "…", "" })
        break
      end
      local detail = common.detail(p)
      local line = common.statusText(p.status)
      if detail ~= "" then
        line = line .. " · " .. detail
      end
      table.insert(rows, { p.name, line })
      shown += 1
    end
  end
  local hide = (noctalia.getConfig("hide_when_ok") == true and overall == "ok") or not s.installed
  barWidget.setVisible(not hide)
  barWidget.setGlyph(common.glyph(overall))
  barWidget.setGlyphColor(common.color(overall))
  barWidget.setText(text)
  barWidget.setColor(common.color(overall))
  barWidget.setTooltip(rows)
end

common.watch(render)

function update()
  render()
end

function onClick()
  noctalia.togglePanel(common.PANEL)
end

function onRightClick()
  local s = common.snapshot()
  if s.running then
    common.send("action", "sync-all")
  else
    common.send("launch")
  end
end

function onMiddleClick()
  if common.snapshot().running then
    common.send("action", "pause-all")
  end
end
