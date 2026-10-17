--!nonstrict
-- Spola control-centre tile: lit while Spola syncs (not paused); click
-- pauses or resumes every folder, or starts Spola when it is not running;
-- right click opens the panel.

local common = require("./lib/common.luau")

local function render()
  local s = common.snapshot()
  shortcut.setIcon(common.GLYPHS.ok, common.GLYPHS.off)
  if not s.running or not s.data then
    shortcut.setActive(false)
    shortcut.setEnabled(true)
    shortcut.setLabel(noctalia.tr("tile.not_running"))
    return
  end
  local d = s.data
  shortcut.setActive(not d.paused_all and d.signed_in)
  shortcut.setEnabled(d.signed_in)
  if d.paused_all then
    shortcut.setLabel(noctalia.tr("tile.paused"))
  elseif d.syncing then
    local p = common.pairById(d, d.syncing)
    shortcut.setLabel(noctalia.tr("tile.syncing", { name = p and p.name or "" }))
  else
    shortcut.setLabel("Spola")
  end
end

common.watch(render)
render()

function onClick()
  if common.snapshot().data then
    common.send("action", "pause-all")
  else
    common.send("launch")
  end
end

function onRightClick()
  noctalia.togglePanel(common.PANEL)
end
