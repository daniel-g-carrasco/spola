--!nonstrict
-- Spola in the launcher: "/spola" lists every folder (sync now, open) and
-- the global actions; in the global search they appear for words like
-- spola, koofr, sync, sincronizza.

local common = require("./lib/common.luau")

local WORDS = { "spola", "koofr", "sync", "sincron", "cloud", "nuvola" }

local function hasWord(q)
  for _, w in WORDS do
    if q:find(w, 1, true) or (#q >= 3 and w:find(q, 1, true) == 1) then
      return true
    end
  end
  return false
end

function onQuery(text)
  local q = noctalia.string.trim(text):lower()
  local s = common.snapshot()
  local rows = {}
  if not s.running then
    if q == "" or hasWord(q) then
      table.insert(rows, { id = "launch", title = noctalia.tr("launcher.start"), subtitle = noctalia.tr("tooltip.not_running"), glyph = common.GLYPHS.off })
    end
    launcher.setResults(text, rows)
    return
  end
  local d = s.data
  local matchesName = false
  for _, p in d.pairs or {} do
    local name = p.name:lower()
    local score = q == "" and 50 or noctalia.fuzzyScore(q, name)
    if score then
      matchesName = true
      local detail = common.detail(p)
      local line = common.statusText(p.status) .. (detail ~= "" and (" · " .. detail) or "")
      if p.needs_you then
        table.insert(rows, { id = "show:" .. p.id, title = noctalia.tr("launcher.review", { name = p.name }), subtitle = line, glyph = common.glyph(p.status), score = score + 20 })
      else
        table.insert(rows, { id = "sync:" .. p["local"], title = noctalia.tr("launcher.sync", { name = p.name }), subtitle = line, glyph = "refresh", score = score + 10 })
        table.insert(rows, { id = "show:" .. p.id, title = noctalia.tr("launcher.open", { name = p.name }), subtitle = line, glyph = "external-link", score = score })
      end
    end
  end
  if q == "" or hasWord(q) or matchesName then
    table.insert(rows, { id = "action:sync-all", title = noctalia.tr("panel.sync_all"), glyph = "refresh", score = 5 })
    table.insert(rows, {
      id = "action:pause-all",
      title = d.paused_all and noctalia.tr("panel.resume_all") or noctalia.tr("panel.pause_all"),
      glyph = d.paused_all and "player-play" or "player-pause",
      score = 4,
    })
    table.insert(rows, { id = "show:", title = noctalia.tr("panel.open_spola"), glyph = common.GLYPHS.ok, score = 3 })
    table.insert(rows, { id = "panel", title = noctalia.tr("launcher.panel"), glyph = "layout-sidebar-right", score = 2 })
  end
  launcher.setResults(text, rows)
end

function onActivate(id)
  if id == "panel" then
    noctalia.togglePanel(common.PANEL)
  elseif id == "launch" then
    common.send("launch")
  else
    local kind, arg = id:match("^(%a+):(.*)$")
    if kind == "sync" then
      common.send("sync", arg)
    elseif kind == "show" then
      common.send("show", arg)
    elseif kind == "action" then
      common.send("action", arg)
    end
  end
end
