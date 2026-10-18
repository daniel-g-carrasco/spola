--!nonstrict
-- Spola service: the only entry that touches the outside. Every two seconds
-- it looks at the date of Spola's status.json and, when it changed, reads it
-- and publishes it (spola.data as JSON text, spola.running); a file not
-- rewritten for three minutes means Spola is gone (it writes a heartbeat
-- every minute). The actions the other entries publish on spola.cmd go to
-- Spola through its D-Bus interface with gdbus.
--
--   noctalia msg plugin daniel-g-carrasco/spola:service all refresh

local common = require("./lib/common.luau")

noctalia.setUpdateInterval(2000)

local STALE_S = 180
local APP = "io.github.daniel_g_carrasco.spola"
local APP_PATH = "/io/github/daniel_g_carrasco/spola"
local SYNC_PATH = "/io/github/daniel_g_carrasco/spola/Sync"
local SYNC_IFACE = APP .. ".Sync1"

local lastMtime = nil
local last = nil        -- the data as last published

local function statusFile()
  local set = noctalia.getConfig("status_file")
  if set and set ~= "" then
    return noctalia.expandPath(set)
  end
  for _, c in {
    "~/.var/app/io.github.daniel_g_carrasco.spola/.local/state/spola/status.json",
    "~/.local/state/spola/status.json",
  } do
    local p = noctalia.expandPath(c)
    if noctalia.fileExists(p) then
      return p
    end
  end
  return nil
end

local function fresh(data)
  return data ~= nil and type(data.updated) == "number" and (os.time() - data.updated) < STALE_S
end

-- Lucciola ships this plugin to everyone: without Spola installed the bar
-- shows nothing and the other entries say so. Looked up once a minute.
local installedAt = 0
local function checkInstalled()
  if os.time() - installedAt < 60 then
    return
  end
  installedAt = os.time()
  local found = noctalia.flatpakAppInstalled(APP) or noctalia.commandExists("spola")
  if noctalia.state.get("spola.installed") ~= found then
    noctalia.state.set("spola.installed", found)
  end
end

local function publish(data)
  last = data
  -- the data first: whoever watches "running" then finds them in place
  noctalia.state.set("spola.data", data and noctalia.json.encode(data) or "")
  noctalia.state.set("spola.running", fresh(data))
end

local function refresh(force)
  local path = statusFile()
  local info = path and noctalia.fileInfo(path) or nil
  if not info then
    if lastMtime ~= nil or last ~= nil then
      lastMtime = nil
      publish(nil)
    end
    return
  end
  if not force and info.mtime == lastMtime then
    -- unchanged: only its age may matter now (Spola gone without a word)
    if last ~= nil and noctalia.state.get("spola.running") == true and not fresh(last) then
      publish(last)
    end
    return
  end
  lastMtime = info.mtime
  noctalia.readFileAsync(path, function(text, err)
    if not text then
      noctalia.log(`spola: status file unreadable: {err}`)
      publish(nil)
      return
    end
    local data = noctalia.json.decode(text)
    publish(type(data) == "table" and data or nil)
  end)
end

function update()
  checkInstalled()
  refresh(false)
end

-- The actions: one gdbus process each, then a fresh look at the file.
-- (Built by appending: table.unpack in the middle of a table keeps one value.)
local function argvFor(args)
  local kind, arg = args[1], tostring(args[2] or "")
  local argv = { "gdbus", "call", "--session", "--dest", APP }
  local function add(...)
    for _, a in { ... } do
      table.insert(argv, a)
    end
  end
  if kind == "show" then
    add("--object-path", SYNC_PATH, "--method", SYNC_IFACE .. ".ShowWindow", arg)
  elseif kind == "sync" then
    add("--object-path", SYNC_PATH, "--method", SYNC_IFACE .. ".SyncNow", arg)
  elseif kind == "action" then
    add("--object-path", APP_PATH, "--method", "org.freedesktop.Application.ActivateAction", arg, "[]", "{}")
  elseif kind == "launch" then
    add("--object-path", APP_PATH, "--method", "org.freedesktop.Application.Activate", "{}")
  else
    return nil
  end
  return argv
end

noctalia.state.watch("spola.cmd", function(raw)
  local req = raw and noctalia.json.decode(raw)
  if type(req) ~= "table" or type(req.args) ~= "table" then
    return
  end
  local argv = argvFor(req.args)
  if not argv then
    return
  end
  if not noctalia.commandExists("gdbus") then
    noctalia.notifyError(noctalia.tr("error.title"), noctalia.tr("error.no_gdbus"))
    return
  end
  noctalia.runAsync(argv, function(r)
    if r.exitCode ~= 0 then
      noctalia.notifyError(noctalia.tr("error.title"), noctalia.string.trim(r.stderr or ""))
    end
    checkInstalled()
refresh(true)
  end)
end)

-- noctalia msg plugin daniel-g-carrasco/spola:service all <refresh|pause|sync-all|show>
function onIpc(event, payload)
  if event == "refresh" then
    refresh(true)
  elseif event == "pause" then
    common.send("action", "pause-all")
  elseif event == "sync-all" then
    common.send("action", "sync-all")
  elseif event == "show" then
    common.send("show", payload or "")
  end
end

refresh(true)
